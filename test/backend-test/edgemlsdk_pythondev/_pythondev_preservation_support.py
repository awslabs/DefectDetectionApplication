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
"""Preservation-baseline helpers for the ``edgemlsdk-python-dev-ubuntu2204``
spec (task 2) — **Property 2: Preservation — All Other Lines and Sibling
Dockerfiles Unchanged**.

Observation-first methodology (mirrors
``edgemlsdk_cmake/_cmake_preservation_support.capture_or_assert_text``): on
the FIRST run each golden is captured from the UNFIXED tree; on every
subsequent run the same view is asserted byte-for-byte against the recorded
golden. The goldens are FROZEN after task 2 — task 5.2 re-runs these tests
unchanged against the fixed tree to prove only the line-286 python-dev
install line changed.

The diff-scoping views (design "Preservation Checking" pseudocode):

* ``mask_pythondev_install_line`` — ``src/edgemlsdk/Dockerfile``'s physical
  lines with ONLY the retired-Python-package install line removed. The
  target step is identified by its apt package tokens: the logical ``RUN``
  instruction whose apt install requests ``python-dev`` (unfixed shape,
  line 286) OR ``python-dev-is-python3`` (the fixed shape from design
  Change 1) — so the SAME frozen golden works before and after the fix.
  No comment header is absorbed: the fix adds/removes no comment, the diff
  is exactly one line. The masked view thereby proves the prior spec's
  CMake block, the Python 3.11 source build, the neighboring
  ``rapidjson-dev libre2-dev`` install, and the ``rm /usr/bin/python`` step
  survive verbatim (Req 3.1).
* full-file ``sha256`` of ``src/edgemlsdk/Dockerfile.jp5`` — byte-for-byte
  untouched post-fix (Req 3.2, design Decision 2).
* full-file ``sha256`` of ``src/edgemlsdk/Dockerfile.jp6`` — byte-for-byte
  untouched post-fix (Req 3.3).

Token-boundary discipline is inherited from ``_pythondev_support``:
``python-dev`` never substring-matches ``python-dev-is-python3`` or
``python3-dev`` — package tokens are whole whitespace-delimited words.

All parsing is TEXT only — no ``docker``, ``subprocess``, or shell-out
anywhere in this package. Import-light so the tests run under
``pytest ... --noconftest``.
"""
import hashlib
import os

from _pythondev_support import (
    RETIRED_TRANSITIONAL_PYTHON_PACKAGES,
    apt_install_packages,
    shell_segments,
)

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINES = os.path.join(HERE, "baselines")

# Golden file names (frozen after task 2 — never rebaseline).
GOLDEN_DOCKERFILE_MASKED = "edgemlsdk_Dockerfile_pythondev_masked.txt"
GOLDEN_JP5_SHA256 = "edgemlsdk_Dockerfile.jp5.sha256.txt"
GOLDEN_JP6_SHA256 = "edgemlsdk_Dockerfile.jp6.sha256.txt"

# The target step's possible package tokens: the UNFIXED shape (line 286's
# retired `python-dev`) OR the FIXED shape (design Change 1's
# `python-dev-is-python3`). Matching either keeps the frozen golden valid on
# both sides of the fix.
TARGET_STEP_TOKENS = frozenset({"python-dev", "python-dev-is-python3"})

# The two admissible shapes of the masked line (mask-exactness content check).
UNFIXED_TRITON_STEP = "RUN apt-get install python-dev -y"
FIXED_TRITON_STEP_SHAPE = "RUN apt-get install python-dev-is-python3 -y"


def baseline_path(name):
    return os.path.join(BASELINES, name)


def capture_or_assert_text(name, current_text):
    """Capture ``current_text`` to the baseline ``name`` when absent (first
    run on the unfixed tree), else assert it still equals the recorded golden
    byte-for-byte. Mirrors
    ``_cmake_preservation_support.capture_or_assert_text``."""
    path = baseline_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(current_text)
        return current_text
    with open(path, encoding="utf-8") as fh:
        recorded = fh.read()
    assert current_text == recorded, (
        f"preservation golden '{name}' changed (F(X) != F'(X)) — a "
        f"non-python-dev line of the observed file differs from the frozen "
        f"unfixed-tree baseline."
    )
    return recorded


def sha256_hex(repo_root, rel_path):
    with open(os.path.join(repo_root, rel_path), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def is_retired_transitional_python_package(token):
    """The retired-token classifier: True iff ``token`` is EXACTLY a member
    of the retired set (never a substring/prefix match — ``python-dev`` is a
    proper prefix of ``python-dev-is-python3``)."""
    return token in RETIRED_TRANSITIONAL_PYTHON_PACKAGES


# --------------------------------------------------------------------------- #
# python-dev install line masking (src/edgemlsdk/Dockerfile)
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
    """Join the physical lines of one RUN instruction into logical text
    (continuation backslashes stripped)."""
    parts = []
    for line in lines[start : end + 1]:
        stripped = line.rstrip()
        if stripped.endswith("\\"):
            stripped = stripped[:-1]
        parts.append(stripped)
    return "\n".join(parts)


def run_step_packages(run_text):
    """All apt-install package tokens of one logical RUN instruction
    (flags excluded, strict token boundaries — via
    ``_pythondev_support.apt_install_packages``)."""
    packages = []
    for seg in shell_segments(run_text):
        pkgs = apt_install_packages(seg)
        if pkgs:
            packages.extend(pkgs)
    return packages


def is_pythondev_install_step(run_text):
    """True iff the logical RUN instruction is the target python-dev install
    step: an apt install requesting ``python-dev`` (unfixed) or
    ``python-dev-is-python3`` (fixed) as a whole package token."""
    return any(p in TARGET_STEP_TOKENS for p in run_step_packages(run_text))


def pythondev_install_line_ranges(lines):
    """Return ``[(start, end)]`` 0-based inclusive physical-line ranges of
    every target python-dev install RUN instruction. No comment header is
    absorbed — the mask covers ONLY the install line(s) (the fix is exactly
    one line, design Change 1)."""
    ranges = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].lstrip().upper().startswith("RUN "):
            end = _run_extent(lines, i)
            if is_pythondev_install_step(_logical_run_text(lines, i, end)):
                ranges.append((i, end))
            i = end + 1
        else:
            i += 1
    return ranges


def mask_pythondev_install_line(lines):
    """Every physical line of a Dockerfile EXCEPT the target python-dev
    install line(s) — the preservation view: identical bytes on the unfixed
    and fixed trees."""
    masked = set()
    for a, b in pythondev_install_line_ranges(lines):
        masked.update(range(a, b + 1))
    return [line for idx, line in enumerate(lines) if idx not in masked]
