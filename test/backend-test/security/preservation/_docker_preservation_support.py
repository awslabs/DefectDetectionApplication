# Copyright 2025 Amazon Web Services, Inc.
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
"""Shared helpers for the **Docker non-ECR base image** preservation baseline
tests (Task 2 of ``security-docker-non-ecr-base-image-fixes``).

These tests implement **Property 2: Preservation — F(X) = F'(X) for every
non-bug-condition input** (design.md "Preservation Checking" / bugfix Req
3.1–3.7). Methodology: **observation-first** — capture the ``F(X)`` baselines on
the UNFIXED tree (task 2, PASS now), then re-run the SAME files against the FIXED
tree (task 8) to prove no legitimate behavior changed and that the only golden
delta per file is the ``FROM`` line(s) plus exactly one added
``ARG BASE_REGISTRY=nvcr.io`` line.

This module reuses the proven low-level helper from the sibling
``_preservation_support`` module (``REPO_ROOT``, ``read_repo_file``) and adds the
Docker-spec-specific extraction / golden helpers:

* ``mask_dockerfile`` — return every line of an in-scope Dockerfile with the
  ``FROM`` lines and any ``ARG BASE_REGISTRY`` line REMOVED. This is the primary
  preservation view: on the unfixed tree there is no ``ARG BASE_REGISTRY`` and the
  ``FROM``s are literal; after the fix the ``FROM``s are parameterized+pinned and
  one ``ARG BASE_REGISTRY`` line is added — but the masked (remaining) lines are
  byte-for-byte identical between the two trees.
* ``default_pull_reference`` / ``parse_from`` — parse each in-scope ``FROM`` and
  compute the current effective default pull reference ``nvcr.io/nvidia/<image>:<tag>``.
* ``resolve`` — the ``BASE_REGISTRY`` resolution model shared by PBT 1.
* ``capture_or_assert_json`` / ``capture_or_assert_text`` — the golden
  capture-or-compare primitive (capture on first run when the baseline is absent,
  assert-equal thereafter).

The in-scope ``docker_base_image_audit`` module (created in task 1) lives one
directory up under ``security/``; ``_ensure_audit_on_path`` puts that directory on
``sys.path`` so PBT 2 can import and exercise the REAL ``is_disallowed_from``.

All helpers are import-light so the tests run under
``python3 -m pytest ... --noconftest`` without pulling in the backend package.
"""
import json
import os
import re
import sys

from _preservation_support import REPO_ROOT, read_repo_file  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
SECURITY_DIR = os.path.normpath(os.path.join(HERE, ".."))
BASELINES = os.path.normpath(os.path.join(HERE, "..", "baselines"))


def _ensure_audit_on_path():
    """Put ``security/`` on sys.path so ``import docker_base_image_audit`` works
    when the tests are run from the ``preservation/`` cwd with ``--noconftest``."""
    if SECURITY_DIR not in sys.path:
        sys.path.insert(0, SECURITY_DIR)


# --------------------------------------------------------------------------- #
# In-scope source paths (relative to REPO_ROOT) this spec owns.
# --------------------------------------------------------------------------- #
BACKEND_JP5_REL = "src/backend/Dockerfile.jp5"
EDGEMLSDK_JP5_REL = "src/edgemlsdk/Dockerfile.jp5"
BACKEND_JP6_REL = "src/backend/Dockerfile.jp6"
EDGEMLSDK_JP6_REL = "src/edgemlsdk/Dockerfile.jp6"

IN_SCOPE_FILES = (
    BACKEND_JP5_REL,
    EDGEMLSDK_JP5_REL,
    BACKEND_JP6_REL,
    EDGEMLSDK_JP6_REL,
)

# The in-scope findings keyed to (file, 1-based line number of the FROM
# instruction). D1-D5 are the original non-ECR base-image findings. D6 is the
# Dockerfile.jp6 TensorRT 8 provider stage (``trt8``, l4t-jetpack:r35.4.1) added
# for the Neo/DLR model runtime (device-arch-compatibility: libnvinfer.so.8); it
# sits between the cuda114 provider (D3, line 19) and the final runtime stage
# (D4), pushing the final FROM from line 20 down to line 33. D6 shares the
# r35.4.1 digest already recorded for D1/D2.
IN_SCOPE_SITES = {
    "D1": (BACKEND_JP5_REL, 5),
    "D2": (EDGEMLSDK_JP5_REL, 2),
    "D3": (BACKEND_JP6_REL, 19),
    "D4": (BACKEND_JP6_REL, 33),
    "D5": (EDGEMLSDK_JP6_REL, 2),
    "D6": (BACKEND_JP6_REL, 32),
}

# The verified multi-arch manifest-list digests the current mutable tags resolve
# to (design.md "The verified digests"). Used by PBT 1 as fixed digests and by
# the default-refs golden so task 8 can assert the fixed default reference equals
# ``nvcr.io/nvidia/<image>:<tag>@sha256:<digest>``.
DIGESTS = {
    ("l4t-jetpack", "r35.4.1"): "sha256:d1c8e971ab994235840eacc31c4ef4173bf9156317b1bf8aabe7e01eb21b2a0e",
    ("l4t-jetpack", "r36.3.0"): "sha256:b3bbd7e3f3a0879a6672adc64aef7742ba12f9baaf1451c91215942c46e4e2fa",
    # r36.4.0: the jp6-vllm-enablement base bump for src/backend/Dockerfile.jp6
    # (D4). The edgemlsdk Dockerfile.jp6 (D5) stays on r36.3.0.
    ("l4t-jetpack", "r36.4.0"): "sha256:34ccf0f3b63c6da9eee45f2e79de9bf7fdf3beda9abfd72bbf285ae9d40bb673",
    ("l4t-cuda", "11.4.19-runtime"): "sha256:fb22ff080631990dda403fd768acb384dc3745a7e516f5ed1dc4c4944898da78",
}

DEFAULT_REGISTRY = "nvcr.io"

# ``FROM <ref> [AS <stage>]`` (ignoring any --flags). Comment lines never match.
_FROM_RE = re.compile(
    r"^\s*FROM\s+(?:--\S+\s+)*(?P<ref>\S+)(?:\s+[Aa][Ss]\s+(?P<stage>\S+))?\s*$"
)
_ARG_BASE_REGISTRY_RE = re.compile(r"^\s*ARG\s+BASE_REGISTRY\b")


def baseline_path(name):
    return os.path.join(BASELINES, name)


# --------------------------------------------------------------------------- #
# FROM-line parsing
# --------------------------------------------------------------------------- #
def parse_from(line):
    """Parse a ``FROM`` line into ``{ref, stage, registry, image, tag, digest}``
    or ``None`` if the line is not a ``FROM`` instruction.

    ``registry`` is the first path component if it looks like a registry host
    (contains a '.' or ':'), else ``None`` (bare library image). ``image`` is the
    last path component's repository name (the part before ``:``/``@``)."""
    m = _FROM_RE.match(line)
    if not m:
        return None
    ref = m.group("ref")
    stage = m.group("stage")
    # Split off any @sha256 digest first.
    digest = None
    body = ref
    if "@" in body:
        body, digest = body.split("@", 1)
    parts = body.split("/")
    registry = None
    if len(parts) > 1 and ("." in parts[0] or ":" in parts[0]):
        registry = parts[0]
        path = parts[1:]
    else:
        path = parts
    last = path[-1]
    tag = None
    if ":" in last:
        image, tag = last.split(":", 1)
    else:
        image = last
    return {
        "ref": ref,
        "stage": stage,
        "registry": registry,
        "image": image,
        "tag": tag,
        "digest": digest,
    }


def in_scope_from_lines(rel_path):
    """Return a list of ``(lineno, line, parsed)`` for every non-comment ``FROM``
    line in an in-scope Dockerfile (1-based line numbers)."""
    text = read_repo_file(rel_path)
    out = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if line.lstrip().startswith("#"):
            continue
        parsed = parse_from(line)
        if parsed is not None:
            out.append((lineno, line, parsed))
    return out


def default_pull_reference(parsed):
    """The current effective default pull reference for a parsed ``FROM`` on the
    UNFIXED tree: ``<registry>/nvidia/<image>:<tag>`` (registry defaults to
    ``nvcr.io`` when parameterized/absent). Digest is intentionally excluded — it
    is the pre-fix reference; task 8 asserts the fixed reference == this + the
    recorded ``@sha256`` digest."""
    registry = parsed["registry"] or DEFAULT_REGISTRY
    ref = f"{registry}/nvidia/{parsed['image']}"
    if parsed["tag"]:
        ref += f":{parsed['tag']}"
    return ref


# --------------------------------------------------------------------------- #
# Masking — the primary preservation view (non-FROM / non-ARG-BASE_REGISTRY bytes)
# --------------------------------------------------------------------------- #
def mask_dockerfile(rel_path):
    """Return the list of lines of ``rel_path`` with every ``FROM`` line and any
    ``ARG BASE_REGISTRY`` line REMOVED (the lines this spec is allowed to touch).

    On the UNFIXED tree there is no ``ARG BASE_REGISTRY`` line, and the ``FROM``s
    are the literal ``nvcr.io`` references. On the FIXED tree the ``FROM``s are
    parameterized+pinned and one ``ARG BASE_REGISTRY=nvcr.io`` line is added.
    Either way the REMAINING lines are byte-for-byte identical — that equality is
    the F(X) = F'(X) preservation guarantee."""
    text = read_repo_file(rel_path)
    kept = []
    for line in text.splitlines():
        if _FROM_RE.match(line):
            continue
        if _ARG_BASE_REGISTRY_RE.match(line):
            continue
        kept.append(line)
    return kept


# --------------------------------------------------------------------------- #
# BASE_REGISTRY resolution model (PBT 1)
# --------------------------------------------------------------------------- #
def resolve(base_registry, image, tag, digest):
    """Model the fixed ``FROM`` reference resolution:

        registry = base_registry or "nvcr.io"
        -> f"{registry}/nvidia/{image}:{tag}@sha256:{digest}"

    ``base_registry`` may be ``None`` (unset -> the ARG default ``nvcr.io``) or a
    concrete override host. ``digest`` is the bare 64-hex (no ``sha256:`` prefix).
    """
    registry = base_registry or DEFAULT_REGISTRY
    return f"{registry}/nvidia/{image}:{tag}@sha256:{digest}"


# --------------------------------------------------------------------------- #
# Golden capture-or-assert primitives
# --------------------------------------------------------------------------- #
def capture_or_assert_json(name, current):
    """Capture ``current`` to the baseline ``name`` (JSON) when it does not exist
    yet (first run on the unfixed tree), else assert it still equals the recorded
    golden. Returns the golden that was compared against.

    ``current`` must be JSON-serializable. On mismatch a clear diff-oriented
    assertion is raised."""
    path = baseline_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(current, fh, indent=2, sort_keys=True)
            fh.write("\n")
        return current
    with open(path, encoding="utf-8") as fh:
        recorded = json.load(fh)
    assert current == recorded, (
        f"preservation golden '{name}' changed (F(X) != F'(X)).\n"
        f"  recorded: {json.dumps(recorded, sort_keys=True)[:2000]}\n"
        f"  current:  {json.dumps(current, sort_keys=True)[:2000]}"
    )
    return recorded


def capture_or_assert_text(name, current_text):
    """Capture ``current_text`` to the baseline ``name`` when absent, else assert
    it still equals the recorded golden byte-for-byte."""
    path = baseline_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(current_text)
        return current_text
    with open(path, encoding="utf-8") as fh:
        recorded = fh.read()
    assert current_text == recorded, (
        f"preservation text golden '{name}' changed (F(X) != F'(X))."
    )
    return recorded
