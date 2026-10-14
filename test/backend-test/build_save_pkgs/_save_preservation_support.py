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
"""Preservation-baseline helpers for the ``build-docker-save-stdout-failure``
spec (task 2) — **Property 2: Preservation — All Other Lines, Tar Paths, and
Neighbor Scripts Unchanged**.

Observation-first methodology (mirrors
``edgemlsdk_pythondev/_pythondev_preservation_support.capture_or_assert_text``):
on the FIRST run each golden is captured from the UNFIXED tree; on every
subsequent run the same view is asserted byte-for-byte against the recorded
golden. The goldens under ``build_save_pkgs/baselines/`` are FROZEN after
task 2 — task 5.2 re-runs these tests unchanged against the fixed tree to
prove that ONLY the one contiguous save block changed.

The diff-scoping views (design "Preservation Checking" pseudocode):

* ``mask_save_block`` — ``build-custom.sh``'s physical lines with ONLY the
  maskable region removed: everything strictly between the two unchanged
  content anchors, the ``echo "save docker images as tarvballs"`` log-anchor
  line above and the ``cp src/docker-compose.yaml ...`` staging line below.
  This is deliberately shape-agnostic so the SAME frozen golden works on
  both sides of the fix: the unfixed shape (the 5-line comment block plus
  the two stdout-redirect save lines) and the fixed shape (the rewritten
  comment block plus the ``save_image_tar`` helper plus the two call lines)
  are both fully contained between those anchors. The region also carries a
  fixed two-line trailer (a blank line and the
  ``# include docker-compose.yaml in archive`` comment) that sits between
  the save block and the compose-``cp`` anchor on BOTH shapes; the
  mask-exactness test pins that trailer's exact bytes so masking it cannot
  hide a collateral edit. The masked view thereby proves the
  interpreter-version audit guard, the edgemlsdk build and deb extraction,
  the docker-compose builds, the in-image backend test and security gate
  block, the staging-dir population, the pre-zip ``rm -f .tmp-*`` cleanup,
  the packaging diagnostics, the ``ZIP_MEMBERS`` list with its exact tar
  paths, the zip invocation with its ``-x '*/.tmp-*'`` exclusion, the
  ``zip -T`` integrity check, and the ``greengrass-build`` copy all survive
  verbatim (Req 3.1, 3.5).
* full-file ``sha256`` goldens of the four scanned neighbor scripts
  (``scripts/portal-build-agent.sh``, ``publish-ecr-only.sh``,
  ``com.dda.InferenceUploader/build-and-publish.sh``,
  ``src/edgemlsdk/build.sh``) — the Req 2.2 class boundary enforced
  mechanically (Req 3.5).

All parsing is TEXT only — no ``docker``, ``subprocess``, or shell-out
anywhere in this package. Import-light so the tests run under
``pytest ... --noconftest``. All anchors are content matches, never line
numbers (the fix shifts later line numbers — sibling precedent).
"""
import hashlib
import os

from _save_support import (
    EXPECTED_UNFIXED_REDIRECT_SITES,
    FLASK_IMAGE,
    FLASK_TAR,
    LOG_ANCHOR_LINE,
    REACT_IMAGE,
    REACT_TAR,
    REPO_ROOT,
    find_helper_body,
    helper_call_sites,
    norm_path,
    stdout_redirect_sites,
)

HERE = os.path.dirname(os.path.abspath(__file__))
BASELINES = os.path.join(HERE, "baselines")

# Golden file names (frozen after task 2 — never rebaseline).
GOLDEN_MASKED = "build_custom_save_masked.txt"


def neighbor_golden_name(rel_path):
    """Deterministic, collision-free golden name for a neighbor script's
    full-file sha256 (path separators flattened)."""
    return rel_path.replace("/", "_") + ".sha256.txt"


# The lower content anchor: the unchanged compose staging line directly
# below the maskable region (the upper anchor is LOG_ANCHOR_LINE).
COMPOSE_CP_LINE = "cp src/docker-compose.yaml ./custom-build/$COMPONENT_NAME/"

# The fixed two-line trailer between the save block and the compose-cp
# anchor — identical bytes on the unfixed and fixed shapes; pinned verbatim
# by the mask-exactness test so masking it hides nothing.
BLOCK_TRAILER = ("", "# include docker-compose.yaml in archive")

# The unfixed save-block shape: 5 comment lines + 2 stdout-redirect saves.
UNFIXED_BLOCK_COMMENT_LINES = 5
UNFIXED_BLOCK_TOTAL_LINES = 7


def baseline_path(name):
    return os.path.join(BASELINES, name)


def capture_or_assert_text(name, current_text):
    """Capture ``current_text`` to the baseline ``name`` when absent (first
    run on the unfixed tree), else assert it still equals the recorded
    golden byte-for-byte. Mirrors the sibling
    ``capture_or_assert_text`` helpers."""
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
        f"non-save-block byte of the observed view differs from the frozen "
        f"unfixed-tree baseline."
    )
    return recorded


def sha256_hex(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


# --------------------------------------------------------------------------- #
# Save-block masking (build-custom.sh) — shape-agnostic, content-anchored
# --------------------------------------------------------------------------- #
def anchor_indices(lines):
    """Locate the two content anchors: the FIRST line stripping to the
    ``echo "save docker images as tarvballs"`` log anchor and the FIRST
    subsequent line stripping to the compose-``cp`` staging line. Returns
    ``(echo_idx, cp_idx)`` (0-based) or None when either anchor is absent —
    total, never raises."""
    echo_idx = None
    for i, line in enumerate(lines):
        if line.strip() == LOG_ANCHOR_LINE:
            echo_idx = i
            break
    if echo_idx is None:
        return None
    for j in range(echo_idx + 1, len(lines)):
        if lines[j].strip() == COMPOSE_CP_LINE:
            return echo_idx, j
    return None


def mask_save_block(lines):
    """The preservation view: every physical line EXCEPT those strictly
    between the two anchors (the anchors themselves are kept — they are
    unchanged by design). Identity when the anchors are absent."""
    found = anchor_indices(lines)
    if found is None:
        return list(lines)
    echo_idx, cp_idx = found
    return list(lines[: echo_idx + 1]) + list(lines[cp_idx:])


def masked_region(lines):
    """The lines the mask removes: everything strictly between the anchors."""
    found = anchor_indices(lines)
    if found is None:
        return []
    echo_idx, cp_idx = found
    return list(lines[echo_idx + 1 : cp_idx])


# --------------------------------------------------------------------------- #
# Block-shape classification (mask exactness — both admissible shapes)
# --------------------------------------------------------------------------- #
def is_unfixed_block(block_lines):
    """True iff ``block_lines`` is exactly the unfixed save block: 5 comment
    lines followed by the two known stdout-redirect save lines (the C(X)
    counterexamples from job d844a5fb)."""
    if len(block_lines) != UNFIXED_BLOCK_TOTAL_LINES:
        return False
    head = block_lines[:UNFIXED_BLOCK_COMMENT_LINES]
    if not all(line.lstrip().startswith("#") for line in head):
        return False
    sites = stdout_redirect_sites("\n".join(block_lines))
    found = {(s.image, norm_path(s.dest)) for s in sites}
    return len(sites) == 2 and found == set(EXPECTED_UNFIXED_REDIRECT_SITES)


def is_fixed_block(block_lines):
    """True iff ``block_lines`` is the fixed save block: zero
    stdout-redirect saves, a ``save_image_tar`` helper definition, and
    exactly the two call sites landing on the exact ZIP_MEMBERS tar paths."""
    text = "\n".join(block_lines)
    if stdout_redirect_sites(text):
        return False
    if find_helper_body(text) is None:
        return False
    calls = helper_call_sites(text)
    if len(calls) != 2 or any(len(args) != 2 for _, args in calls):
        return False
    dest_by_image = {args[0]: norm_path(args[1]) for _, args in calls}
    return dest_by_image == {FLASK_IMAGE: FLASK_TAR, REACT_IMAGE: REACT_TAR}


def image_save_final_destinations(text):
    """The (image, normalized final destination) pairs of the image-save
    sites, shape-agnostically: the stdout-redirect targets on the unfixed
    tree, the ``save_image_tar`` call-site destinations on the fixed tree."""
    sites = stdout_redirect_sites(text)
    if sites:
        return {(s.image, norm_path(s.dest)) for s in sites}
    return {
        (args[0], norm_path(args[1]))
        for _, args in helper_call_sites(text)
        if len(args) == 2
    }
