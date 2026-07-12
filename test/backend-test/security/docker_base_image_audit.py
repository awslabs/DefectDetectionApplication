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
"""Repo audit for the Docker non-ECR base-image bug-condition patterns
(security-docker-non-ecr-base-image-fixes, finding D6 / Req 2.6).

This is the companion gate to the sibling ``repo_audit.py`` (Group 1 --
injection / deserialization), ``secrets_audit.py`` (Group 3 -- secrets / JWT),
``iam_audit.py`` (Group 4 -- IAM / authorization), and ``s3_squat_audit.py``
(Group 5 -- S3 bucket squatting). It owns a DIFFERENT set of patterns (base-image
``FROM`` lines in the four maintained Jetson Dockerfiles that pull from the
external, non-ECR registry ``nvcr.io`` and are not both registry-parameterized
via a ``${BASE_REGISTRY...}`` ARG AND digest-pinned with ``@sha256:``) and a
different in-scope file set, so it is deliberately a separate module rather than
an edit to the siblings' already-green gates. To avoid duplication it REUSES the
siblings' proven low-level primitives (``REPO_ROOT``, ``EXCLUDED_PATH_SUBSTRING``,
``Hit``) via the SAME try/except fallback re-implementation the siblings use when
``repo_audit`` is not importable, and defines only its OWN constants, the
``FROM``-line classification, and the precise ``disallowed_hits()`` logic. (A
string-based ``_has_nosem`` is re-implemented locally because the sibling
``repo_audit._has_nosem`` keys on a ``Hit`` object, whereas this gate classifies
a bare ``FROM``-reference string.)

Two layers that intentionally differ in breadth (mirroring the siblings):

* ``run_audit()`` -- the RAW, broad, line-based enumeration. It scans the four
  in-scope Dockerfiles for every non-comment ``FROM`` line and emits a ``Hit``
  per ``FROM`` (``FROM <ref> [AS <stage>]``). It applies NO precision /
  classification filtering beyond ``IN_SCOPE_FILES`` scoping -- that is by design
  for the exploration phase. On the UNFIXED tree it surfaces the five in-scope
  ``FROM``s (D1-D5, the counterexamples that confirm the bug); task 1 uses it to
  list them. It NEVER scans the vendored ``src/backend/edgemlsdk/edgemlsdk/...``
  duplicate or non-Dockerfiles.

* ``disallowed_hits()`` -- the PRECISE per-``FROM`` post-fix gate re-run in
  task 7. For each ``FROM`` in the in-scope files it keys on the ``FROM``
  *reference string* (the portion after ``FROM``, before any ``AS <stage>``):
  a ``FROM`` is cleared when it carries a ``# nosec``/``# nosem`` marker, when
  the ref is an ECR host, or when the ref is BOTH ``${BASE_REGISTRY...}``-
  parameterized AND ``@sha256:``-digest-pinned; otherwise it is disallowed iff
  the ref references a non-ECR literal registry (e.g. ``nvcr.io``) AND is either
  not parameterized OR not digest-pinned. The classification is PER-``FROM``,
  not file-global: a compliant ``FROM ${BASE_REGISTRY}/nvidia/l4t-jetpack:...@sha256:...``
  line is cleared even though the file's ``ARG BASE_REGISTRY=nvcr.io`` default
  still contains ``nvcr.io`` elsewhere. On the UNFIXED tree this returns the five
  D1-D5 disallowed ``FROM``s; after the fix it must return ``[]``.

In-scope scoping: both layers are asserted ONLY over ``IN_SCOPE_FILES`` (the four
maintained Jetson Dockerfiles this spec owns), so neither matches the vendored
``src/backend/edgemlsdk/edgemlsdk/...`` duplicate (also excluded defensively via
an ``os.path.join("edgemlsdk", "edgemlsdk")`` substring check), the generated
``cdk.out`` artifacts, nor the already-compliant ``public.ecr.aws/*`` Dockerfiles.
"""
import os
import re

# Reuse the sibling module's proven low-level primitives where sensible. If the
# import is unavailable in some runner, fall back to a thin re-implementation so
# this module stays self-contained.
try:
    from repo_audit import (  # type: ignore
        REPO_ROOT,
        EXCLUDED_PATH_SUBSTRING,
        Hit,
    )
except Exception:  # pragma: no cover - fallback when repo_audit is not importable
    from collections import namedtuple

    REPO_ROOT = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
    )
    EXCLUDED_PATH_SUBSTRING = os.path.join("cdk.out", "asset.")
    Hit = namedtuple("Hit", ["category", "path", "lineno", "text"])


# Defensive substring identifying the vendored / generated duplicate SDK subtree
# (src/backend/edgemlsdk/edgemlsdk/...). It regenerates from the maintained
# source at src/edgemlsdk/... by build-custom.sh and is explicitly out of scope.
VENDORED_DUP_SUBSTRING = os.path.join("edgemlsdk", "edgemlsdk")

# ---------------------------------------------------------------------------
# In-scope files (relative to REPO_ROOT, normpath'd) -- the four maintained
# Jetson Dockerfiles this spec owns. The vendored duplicate and the
# already-compliant public.ecr.aws/* Dockerfiles are NOT in scope.
# ---------------------------------------------------------------------------
BACKEND_JP5_REL = os.path.normpath(os.path.join("src", "backend", "Dockerfile.jp5"))
EDGEMLSDK_JP5_REL = os.path.normpath(os.path.join("src", "edgemlsdk", "Dockerfile.jp5"))
BACKEND_JP6_REL = os.path.normpath(os.path.join("src", "backend", "Dockerfile.jp6"))
EDGEMLSDK_JP6_REL = os.path.normpath(os.path.join("src", "edgemlsdk", "Dockerfile.jp6"))

IN_SCOPE_FILES = frozenset(
    (BACKEND_JP5_REL, EDGEMLSDK_JP5_REL, BACKEND_JP6_REL, EDGEMLSDK_JP6_REL)
)

# The five in-scope findings -> the real source file that carries them (NOT the
# vendored edgemlsdk/edgemlsdk/... copies). D3 and D4 both live in jp6 backend.
IN_SCOPE_SITES = {
    "D1 backend jp5 l4t-jetpack:r35.4.1": BACKEND_JP5_REL,
    "D2 edgemlsdk jp5 l4t-jetpack:r35.4.1 AS builder": EDGEMLSDK_JP5_REL,
    "D3+D4 backend jp6 l4t-cuda AS cuda114 + l4t-jetpack:r36.3.0": BACKEND_JP6_REL,
    "D5 edgemlsdk jp6 l4t-jetpack:r36.3.0 AS builder": EDGEMLSDK_JP6_REL,
}

# ---------------------------------------------------------------------------
# Classification regexes (applied to the FROM *reference string*).
# ---------------------------------------------------------------------------
# An ECR host: public.ecr.aws OR <account-digits>.dkr.ecr.<region>.amazonaws.com.
_ECR_HOST_RE = re.compile(
    r"public\.ecr\.aws|\d+\.dkr\.ecr\.[a-z0-9-]+\.amazonaws\.com"
)
# The registry-parameterization ARG seam: ${BASE_REGISTRY}, ${BASE_REGISTRY:-...},
# or $BASE_REGISTRY. Its default value may legitimately BE nvcr.io.
_PARAMETERIZED_REGISTRY_RE = re.compile(r"\$\{?BASE_REGISTRY[:}]?")
# An immutable digest pin.
_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{64}\b")
# A FROM instruction: capture the reference (after FROM / any --flags, before an
# optional `AS <stage>`). Comment-only lines never match (they start with '#').
_FROM_RE = re.compile(
    r"^\s*FROM\s+(?:--\S+\s+)*(?P<ref>\S+)(?:\s+[Aa][Ss]\s+(?P<stage>\S+))?\s*$"
)


def _rel(path):
    return os.path.normpath(os.path.relpath(path, REPO_ROOT))


def _read(rel_path):
    """Read an in-scope file; return "" if it does not exist."""
    abs_path = os.path.join(REPO_ROOT, rel_path)
    try:
        with open(abs_path, encoding="utf-8", errors="replace") as f:
            return f.read()
    except FileNotFoundError:  # pragma: no cover
        return ""


def _has_nosem(text):
    """True if the line carries a documented suppression marker."""
    low = text.lower()
    return "nosem" in low or "nosec" in low or "noqa" in low


def _excluded_path(rel_path):
    """True if a path is one of the out-of-scope trees (the vendored duplicate
    or a cdk.out artifact)."""
    return VENDORED_DUP_SUBSTRING in rel_path or EXCLUDED_PATH_SUBSTRING in rel_path


def _is_comment_line(text):
    """True if the source line is a pure comment (no live FROM)."""
    return text.lstrip().startswith("#")


def from_reference(line):
    """Return the FROM reference string (the portion after FROM, before any
    ``AS <stage>``) for a FROM line, or None if the line is not a FROM."""
    m = _FROM_RE.match(line)
    if not m:
        return None
    return m.group("ref")


def from_stage(line):
    """Return the ``AS <stage>`` name for a FROM line, or None."""
    m = _FROM_RE.match(line)
    if not m:
        return None
    return m.group("stage")


def _references_nonecr_literal_registry(ref):
    """True if the FROM reference names a non-ECR *literal* registry host.

    Keyed on the reference string only. A ``${BASE_REGISTRY...}``-parameterized
    registry is NOT a literal registry (returns False). A first path component
    that looks like a registry host (contains a '.' or ':' port, or is
    'localhost') and is not an ECR host counts as a non-ECR literal registry."""
    if _PARAMETERIZED_REGISTRY_RE.search(ref):
        # The registry portion is parameterized, not a literal.
        return False
    registry = ref.split("/", 1)[0]
    is_host = ("." in registry) or (":" in registry) or registry == "localhost"
    if not is_host:
        # No registry component (e.g. a bare `ubuntu:22.04` / library image).
        return False
    return not bool(_ECR_HOST_RE.search(registry))


def is_disallowed_from(line):
    """Precise per-FROM gate for a single FROM line.

    Cleared (allowed) when: not a FROM line; carries a # nosec/# nosem marker;
    the ref is an ECR host; or the ref is BOTH ${BASE_REGISTRY...}-parameterized
    AND @sha256:-digest-pinned. Otherwise disallowed iff the ref references a
    non-ECR literal registry AND (is not parameterized OR is not digest-pinned).
    """
    ref = from_reference(line)
    if ref is None:
        return False
    if _has_nosem(line):
        return False
    if _ECR_HOST_RE.search(ref):
        return False
    parameterized = bool(_PARAMETERIZED_REGISTRY_RE.search(ref))
    pinned = bool(_DIGEST_RE.search(ref))
    if parameterized and pinned:
        return False
    if _references_nonecr_literal_registry(ref) and (not parameterized or not pinned):
        return True
    return False


# ---------------------------------------------------------------------------
# Layer 1 -- raw, broad, line-based enumeration of every in-scope FROM.
# ---------------------------------------------------------------------------
def run_audit():
    """Return a Hit per non-comment FROM line across the four in-scope
    Dockerfiles (the vendored duplicate and cdk.out are never scanned). No
    classification / exception filtering is applied -- this is the raw
    bug-condition enumeration used by the exploration test. Non-empty on the
    unfixed tree (the five in-scope FROMs D1-D5)."""
    hits = []
    for rel_path in sorted(IN_SCOPE_FILES):
        if _excluded_path(rel_path):
            continue
        abs_path = os.path.join(REPO_ROOT, rel_path)
        text = _read(rel_path)
        if not text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _is_comment_line(line):
                continue
            if from_reference(line) is None:
                continue
            hits.append(Hit(category="from_line", path=abs_path,
                            lineno=lineno, text=line))
    return hits


# ---------------------------------------------------------------------------
# Layer 2 -- precise per-FROM post-fix gate.
# ---------------------------------------------------------------------------
def disallowed_hits():
    """The PRECISE post-fix per-FROM gate (task 7).

    A FROM is *disallowed* only when it is in an in-scope Dockerfile, carries no
    documented ``# nosec`` / ``# nosem`` exception, and references a non-ECR
    literal registry that is NOT both ${BASE_REGISTRY...}-parameterized AND
    ``@sha256:``-digest-pinned. Classification is per-FROM (keyed on the
    reference string), NOT file-global.

    On the UNFIXED tree this is non-empty (the five D1-D5 counterexamples); after
    the fix it must be empty (minus documented exceptions)."""
    hits = []
    for rel_path in sorted(IN_SCOPE_FILES):
        if _excluded_path(rel_path):
            continue
        abs_path = os.path.join(REPO_ROOT, rel_path)
        text = _read(rel_path)
        if not text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _is_comment_line(line):
                continue
            if is_disallowed_from(line):
                ref = from_reference(line)
                hits.append(Hit(
                    "non_ecr_base_image", abs_path, lineno,
                    f"non-ECR base image '{ref}' is not both "
                    f"${{BASE_REGISTRY}}-parameterized and @sha256-pinned: "
                    f"{line.strip()}"))
    return hits


def hits_for(path_substring, hits=None):
    """All hits whose file path contains ``path_substring``."""
    hits = run_audit() if hits is None else hits
    return [h for h in hits if path_substring in h.path]


def disallowed_by_category(hits=None):
    """Group disallowed hits by category."""
    hits = disallowed_hits() if hits is None else hits
    grouped = {}
    for h in hits:
        grouped.setdefault(h.category, []).append(h)
    return grouped


if __name__ == "__main__":
    all_hits = run_audit()
    print(f"Docker non-ECR base-image audit: {len(all_hits)} raw FROM line(s) "
          f"(vendored edgemlsdk/edgemlsdk & cdk.out excluded)\n")
    for label, frag in IN_SCOPE_SITES.items():
        site_hits = hits_for(frag, all_hits)
        print(f"=== {label} ({frag}) : {len(site_hits)} raw FROM(s) ===")
        for h in site_hits:
            print(f"  [{h.category}] {_rel(h.path)}:{h.lineno}: {h.text.strip()}")
        print()

    disallowed = disallowed_hits()
    grouped = disallowed_by_category(disallowed)
    print("-" * 70)
    print(f"PATTERN GATE: {len(disallowed)} disallowed FROM(s) in in-scope "
          f"Dockerfiles (must be 0 after fix, minus documented exceptions).")
    for category in sorted(grouped):
        print(f"  --- {category}: {len(grouped[category])} hit(s) ---")
        for h in grouped[category]:
            print(f"    {_rel(h.path)}:{h.lineno}: {h.text.strip()}")
    raise SystemExit(1 if disallowed else 0)
