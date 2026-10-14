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
"""Preservation-baseline helpers for the ``backend-jammy-retired-packages``
spec (task 2) — **Property 2: Preservation — All Other Lines, Old-Base
Behavior, and Sibling Files Unchanged**.

Observation-first methodology (mirrors
``edgemlsdk_pythondev/_pythondev_preservation_support.capture_or_assert_text``):
on the FIRST run each golden is captured from the UNFIXED tree; on every
subsequent run the same view is asserted byte-for-byte against the recorded
golden. The goldens are FROZEN after task 2 — task 5.2 re-runs these tests
unchanged against the fixed tree to prove only the line-70 libssl install
step changed.

The diff-scoping views (design "Preservation Checking" pseudocode):

* ``mask_libssl_install_step`` — ``src/backend/Dockerfile``'s physical lines
  with ONLY the libssl install step removed. The target step is identified
  by its apt package tokens: the logical ``RUN`` instruction whose apt
  install requests the exact token ``libssl1.1`` (content-matched, never
  line-number-matched). **The mask matches BOTH shapes**: the unfixed single
  line ``RUN apt-get install libssl1.1 -y`` (line 70) AND the fixed
  comment-block-plus-``/etc/os-release``-conditional form from design
  Change 1 — any CONTIGUOUS comment header directly above the target RUN is
  absorbed into the masked region, so the fixed form's traveling comment
  block is masked too and the SAME frozen golden works before and after the
  fix. On the unfixed tree the line directly above the target is line 69's
  ``RUN apt update -y`` (NOT a comment), so nothing extra is absorbed — and
  because absorption only ever happens directly above a *target* step, no
  unrelated comment anywhere else in the file is ever masked.
* full-file ``sha256`` goldens of the 8 untouched files:
  ``src/frontend/Dockerfile``, ``src/docker-compose.yaml``,
  ``src/backend/Dockerfile.jp5``, ``.jp6``, ``.x86_64_nvidia`` (Req 3.3),
  and the three install scripts ``prereqs_install.sh``,
  ``install_aravis.sh``, ``install_edgemlsdk.sh`` (design Decision 3,
  enforced mechanically).

Token-boundary discipline is inherited from ``_jammy_support``:
``libssl1.1`` only ever matches as an exact whole package token —
``libssl-dev`` (line 4), ``libssl3`` (jammy's runtime), and adversarial
near-misses like ``libssl1.1-foo`` never classify as retired and never
trigger the mask.

All parsing is TEXT only — no ``docker``, ``subprocess``, or shell-out
anywhere in this package. Import-light so the tests run under
``pytest ... --noconftest``.
"""
import hashlib
import os

from _jammy_support import (
    ARAVIS_SCRIPT_REL,
    BUG_SITE_TOKEN,
    EDGEMLSDK_SCRIPT_REL,
    FIXED_LIBSSL_ALLOWLIST,
    FIXED_LIBSSL_BODY,
    FRONTEND_DOCKERFILE_REL,
    PREREQS_SCRIPT_REL,
    RETIRED_JAMMY_PACKAGES,
    UNFIXED_BUG_STEP,
    apt_install_packages,
    guard_allowlist,
    parse_run_instructions,
    shell_segments,
)

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINES = os.path.join(HERE, "baselines")

# Golden file names (frozen after task 2 — never rebaseline).
GOLDEN_BACKEND_MASKED = "backend_Dockerfile_libssl_masked.txt"

# The 8 files the fix must leave byte-for-byte untouched (Req 3.3 +
# design Decision 3), each pinned by a full-file sha256 golden.
DOCKER_COMPOSE_REL = "src/docker-compose.yaml"
BACKEND_JP5_REL = "src/backend/Dockerfile.jp5"
BACKEND_JP6_REL = "src/backend/Dockerfile.jp6"
BACKEND_X86_NVIDIA_REL = "src/backend/Dockerfile.x86_64_nvidia"

UNTOUCHED_FILES = (
    (FRONTEND_DOCKERFILE_REL, "frontend_Dockerfile.sha256.txt"),
    (DOCKER_COMPOSE_REL, "docker-compose.yaml.sha256.txt"),
    (BACKEND_JP5_REL, "backend_Dockerfile.jp5.sha256.txt"),
    (BACKEND_JP6_REL, "backend_Dockerfile.jp6.sha256.txt"),
    (BACKEND_X86_NVIDIA_REL, "backend_Dockerfile.x86_64_nvidia.sha256.txt"),
    (PREREQS_SCRIPT_REL, "prereqs_install.sh.sha256.txt"),
    (ARAVIS_SCRIPT_REL, "install_aravis.sh.sha256.txt"),
    (EDGEMLSDK_SCRIPT_REL, "install_edgemlsdk.sh.sha256.txt"),
)


def baseline_path(name):
    return os.path.join(BASELINES, name)


def capture_or_assert_text(name, current_text):
    """Capture ``current_text`` to the baseline ``name`` when absent (first
    run on the unfixed tree), else assert it still equals the recorded golden
    byte-for-byte. Mirrors
    ``_pythondev_preservation_support.capture_or_assert_text``."""
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
        f"non-libssl-step byte of the observed view differs from the frozen "
        f"unfixed-tree baseline."
    )
    return recorded


def sha256_hex(repo_root, rel_path):
    with open(os.path.join(repo_root, rel_path), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def is_retired_jammy_package(token):
    """The retired-token classifier: True iff ``token`` is EXACTLY a member
    of the retired set (never a substring/prefix/suffix match —
    ``libssl-dev``, ``libssl3``, and ``libssl1.1-foo`` are never retired)."""
    return token in RETIRED_JAMMY_PACKAGES


# --------------------------------------------------------------------------- #
# Text-based apt-step parsing (for generated/masked Dockerfile text)
# --------------------------------------------------------------------------- #
def apt_steps_in_text(dockerfile_text):
    """Every apt install step of Dockerfile TEXT as
    ``(start_lineno, logical_text, packages, allowlist)`` tuples — the same
    pipeline as ``_jammy_support.dockerfile_apt_steps`` but over a string,
    for masked views and Hypothesis-generated sequences."""
    steps = []
    for lineno, text in parse_run_instructions(dockerfile_text):
        cmd = text.lstrip()[len("RUN "):]
        packages = []
        for seg in shell_segments(cmd):
            pkgs = apt_install_packages(seg)
            if pkgs:
                packages.extend(pkgs)
        if packages:
            steps.append((lineno, text, packages, guard_allowlist(text)))
    return steps


def retired_sites_in_text(dockerfile_text, base="22.04"):
    """The retired-token scan over Dockerfile TEXT: every
    ``(lineno, token)`` where an apt install step reachable on ``base``
    requests a jammy-retired package as a whole token."""
    sites = []
    for lineno, _text, packages, allowlist in apt_steps_in_text(
        dockerfile_text
    ):
        if allowlist is not None and base not in allowlist:
            continue
        for pkg in packages:
            if is_retired_jammy_package(pkg):
                sites.append((lineno, pkg))
    return sites


# --------------------------------------------------------------------------- #
# libssl install step masking (src/backend/Dockerfile) — shape-agnostic
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
    ``_jammy_support.apt_install_packages``)."""
    packages = []
    for seg in shell_segments(run_text):
        pkgs = apt_install_packages(seg)
        if pkgs:
            packages.extend(pkgs)
    return packages


def is_libssl_install_step(run_text):
    """True iff the logical RUN instruction is the target libssl install
    step: an apt install requesting the exact token ``libssl1.1`` (unfixed
    unconditional shape OR the fixed guarded shape — both request the same
    token, so the same predicate identifies both)."""
    return BUG_SITE_TOKEN in run_step_packages(run_text)


def libssl_step_line_ranges(lines):
    """Return ``[(start, end)]`` 0-based inclusive physical-line ranges of
    every target libssl install step, WITH any contiguous comment header
    directly above the RUN absorbed into the range.

    Comment absorption is the load-bearing both-shapes subtlety (design
    Change 1): the fixed form's comment block travels with the step inside
    the single masked region. On the unfixed tree the line directly above
    the target is ``RUN apt update -y`` (line 69) — not a comment — so
    nothing extra is absorbed and the masked region is exactly the one
    physical line. Absorption happens ONLY directly above a target step, so
    unrelated comments elsewhere are never masked."""
    ranges = []
    i = 0
    n = len(lines)
    while i < n:
        if lines[i].lstrip().upper().startswith("RUN "):
            end = _run_extent(lines, i)
            if is_libssl_install_step(_logical_run_text(lines, i, end)):
                start = i
                while start > 0 and lines[start - 1].lstrip().startswith("#"):
                    start -= 1
                ranges.append((start, end))
            i = end + 1
        else:
            i += 1
    return ranges


def mask_libssl_install_step(lines):
    """Every physical line of a Dockerfile EXCEPT the target libssl install
    step(s) and their contiguous comment headers — the preservation view:
    identical bytes on the unfixed and fixed trees."""
    masked = set()
    for a, b in libssl_step_line_ranges(lines):
        masked.update(range(a, b + 1))
    return [line for idx, line in enumerate(lines) if idx not in masked]


# --------------------------------------------------------------------------- #
# Mask-exactness shape classification (both admissible shapes)
# --------------------------------------------------------------------------- #
def _apt_bodies(logical_run_text):
    """The normalized apt command bodies of one logical RUN instruction —
    whitespace collapsed and leading shell keywords (``then``/``else``/
    ``do``) stripped, so the fixed guarded form's body compares equal to
    ``FIXED_LIBSSL_BODY``."""
    cmd = logical_run_text.lstrip()[len("RUN "):]
    bodies = []
    for seg in shell_segments(cmd):
        if apt_install_packages(seg):
            toks = seg.split()
            while toks and toks[0] in ("then", "else", "do"):
                toks = toks[1:]
            bodies.append(" ".join(toks))
    return bodies


def masked_block_shape(block_lines):
    """Classify a masked physical-line block as one of the two admissible
    shapes, or None:

    - ``"unfixed"``: exactly one physical line, no comment header, stripped
      text exactly ``RUN apt-get install libssl1.1 -y`` (the line-70 bug
      step);
    - ``"fixed"``: zero-or-more contiguous comment lines followed by a
      single RUN instruction that sources ``/etc/os-release``, guards on
      ``"$VERSION_ID"`` with allowlist exactly {"18.04", "20.04"}, and whose
      only apt body is exactly ``apt-get install libssl1.1 -y`` (design
      Change 1's comment-block-plus-conditional form).
    """
    comments = 0
    while comments < len(block_lines) and block_lines[comments].lstrip(
    ).startswith("#"):
        comments += 1
    run_lines = block_lines[comments:]
    if not run_lines or not run_lines[0].lstrip().upper().startswith("RUN "):
        return None
    if _run_extent(run_lines, 0) != len(run_lines) - 1:
        return None
    logical = _logical_run_text(run_lines, 0, len(run_lines) - 1)
    if not is_libssl_install_step(logical):
        return None
    if (
        comments == 0
        and len(run_lines) == 1
        and run_lines[0].strip() == UNFIXED_BUG_STEP
    ):
        return "unfixed"
    if (
        guard_allowlist(logical) == FIXED_LIBSSL_ALLOWLIST
        and ". /etc/os-release" in logical
        and _apt_bodies(logical) == [FIXED_LIBSSL_BODY]
    ):
        return "fixed"
    return None
