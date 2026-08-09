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
"""Preservation-baseline helpers for the ``edgemlsdk-cmake-pin-failure`` spec
(task 2) — **Property 2: Preservation — Non-CMake Lines and JP6 Unchanged**.

Observation-first methodology (mirrors
``security/preservation/_docker_preservation_support.capture_or_assert_text``):
on the FIRST run each golden is captured from the UNFIXED tree; on every
subsequent run the same view is asserted byte-for-byte against the recorded
golden. The goldens are FROZEN after task 2 — task 5.2 re-runs these tests
unchanged against the fixed tree to prove only the CMake install lines changed.

The diff-scoping views (design "Preservation Checking" pseudocode):

* ``sha256`` of the full ``src/edgemlsdk/Dockerfile.jp6`` bytes — the JP6 file
  must remain bit-identical post-fix (Req 3.1).
* ``mask_cmake_install_blocks`` — a Dockerfile's physical lines with each
  **CMake install block** removed. A block is a ``RUN`` instruction (all of
  its backslash-continuation physical lines) that installs CMake per the
  task-1 ``is_cmake_install_step`` predicate, PLUS the contiguous comment
  lines immediately above it (the block's comment header). The header is part
  of the block by design: the fix replaces the whole commented block (design
  Changes 1–2 include a new explanatory comment), so both the old and the new
  header/RUN text must be masked for the remaining bytes to be comparable
  across the unfixed and fixed trees.
* ``mask_shell_function`` — ``machine_setup.sh`` with the entire
  ``install_cmake`` function (signature through its closing ``}`` at column 0)
  removed. The fix rewrites the function's guard and its 18.04 branch body, so
  the whole function is the allowed-to-change region; every other line of the
  script must survive byte-for-byte (Req 3.5).

Import-light so the tests run under ``pytest ... --noconftest``.
"""
import hashlib
import os
import re

from _cmake_support import is_cmake_install_step

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINES = os.path.join(HERE, "baselines")

# Golden file names (frozen after task 2 — never rebaseline).
GOLDEN_JP6_SHA256 = "edgemlsdk_Dockerfile.jp6.sha256.txt"
GOLDEN_DOCKERFILE_MASKED = "edgemlsdk_Dockerfile_cmake_masked.txt"
GOLDEN_DOCKERFILE_JP5_MASKED = "edgemlsdk_Dockerfile.jp5_cmake_masked.txt"
GOLDEN_MACHINE_SETUP_MASKED = "machine_setup.sh_install_cmake_masked.txt"


def baseline_path(name):
    return os.path.join(BASELINES, name)


def capture_or_assert_text(name, current_text):
    """Capture ``current_text`` to the baseline ``name`` when absent (first run
    on the unfixed tree), else assert it still equals the recorded golden
    byte-for-byte. Mirrors
    ``_docker_preservation_support.capture_or_assert_text``."""
    path = baseline_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(current_text)
        return current_text
    with open(path, encoding="utf-8") as fh:
        recorded = fh.read()
    assert current_text == recorded, (
        f"preservation golden '{name}' changed (F(X) != F'(X)) — a non-CMake "
        f"line of the observed file differs from the frozen unfixed-tree "
        f"baseline."
    )
    return recorded


def sha256_hex(repo_root, rel_path):
    with open(os.path.join(repo_root, rel_path), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# --------------------------------------------------------------------------- #
# CMake-install-block masking (Dockerfiles)
# --------------------------------------------------------------------------- #
def _run_extent(lines, start):
    """Inclusive physical end index of the RUN instruction starting at
    ``start`` (backslash continuation lines, Dockerfile syntax)."""
    j = start
    n = len(lines)
    while j < n - 1 and lines[j].rstrip().endswith("\\"):
        j += 1
    return j


def _logical_run_text(lines, start, end):
    """Join the physical lines of one RUN instruction into the logical text the
    task-1 ``is_cmake_install_step`` predicate expects (continuation
    backslashes stripped)."""
    parts = []
    for line in lines[start : end + 1]:
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            stripped = stripped[:-1]
        parts.append(stripped)
    return "\n".join(parts)


def cmake_install_block_ranges(lines):
    """Return ``[(start, end)]`` 0-based inclusive physical-line ranges of every
    CMake install block: the CMake-installing ``RUN`` instruction plus its
    contiguous comment-header lines directly above."""
    ranges = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].lstrip().upper().startswith("RUN "):
            end = _run_extent(lines, i)
            if is_cmake_install_step(_logical_run_text(lines, i, end)):
                k = i - 1
                while k >= 0 and lines[k].lstrip().startswith("#"):
                    k -= 1
                ranges.append((k + 1, end))
            i = end + 1
        else:
            i += 1
    return ranges


def mask_cmake_install_blocks(lines):
    """Every physical line of a Dockerfile EXCEPT the CMake install block(s) —
    the preservation view: identical bytes on the unfixed and fixed trees."""
    masked = set()
    for a, b in cmake_install_block_ranges(lines):
        masked.update(range(a, b + 1))
    return [line for idx, line in enumerate(lines) if idx not in masked]


# --------------------------------------------------------------------------- #
# install_cmake function masking (machine_setup.sh)
# --------------------------------------------------------------------------- #
def shell_function_range(lines, name):
    """0-based inclusive ``(start, end)`` of shell function ``name`` — from the
    ``name()`` signature line through its closing ``}`` at column 0."""
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^{re.escape(name)}\s*\(\)", line):
            start = i
            break
    assert start is not None, f"function {name}() not found"
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("}"):
            return start, j
    raise AssertionError(f"closing brace of {name}() not found")


def mask_shell_function(lines, name):
    """Every line of the script EXCEPT function ``name`` (the allowed-to-change
    region of ``machine_setup.sh``)."""
    start, end = shell_function_range(lines, name)
    return lines[:start] + lines[end + 1 :]
