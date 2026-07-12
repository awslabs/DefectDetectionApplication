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
"""Shared helpers for the **dependency / supply-chain CVE** preservation baseline
tests (Task 2 of ``security-dependency-cve-fixes``).

These tests implement **Property 2: Preservation -- F(X) = F'(X) for every
non-bug-condition input** (design.md "Preservation Checking" / bugfix Req
3.1-3.7). Methodology: **observation-first** -- capture the ``F(X)`` baselines on
the UNFIXED tree (task 2, PASS now), then re-run the SAME files against the FIXED
tree (task 7) to prove the ONLY delta is the two ``2.32.3`` -> ``2.32.4`` version
tokens at ``setup_station.sh:513`` and ``requirements.txt:9``; every other byte
-- most critically the UNPINNED Python-3.6 host installs -- is unchanged.

This module reuses the proven low-level helper from the sibling
``_preservation_support`` module (``REPO_ROOT``, ``read_repo_file``) and adds the
dependency-spec-specific location / golden helpers. The in-scope
``dependency_audit`` module (created in task 1) lives one directory up under
``security/``; ``_ensure_audit_on_path`` puts that directory on ``sys.path`` so
the tests + PBTs can import and exercise the REAL constants / predicates
(``SETUP_STATION_REL``, ``BACKEND_REQS_REL``, ``_pin_is_disallowed``,
``_parse_version``, ``_REQUESTS_PIN_RE``, ``IN_SCOPE_PIN_FILES``,
``VENDORED_DIGEST_AUTH_REL``, ``_has_nosem``).

All helpers are import-light so the tests run under
``python3 -m pytest ... --noconftest`` without pulling in the backend package.
"""
import hashlib
import json
import os
import sys

from _preservation_support import REPO_ROOT, read_repo_file  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
SECURITY_DIR = os.path.normpath(os.path.join(HERE, ".."))
BASELINES = os.path.normpath(os.path.join(HERE, "..", "baselines"))


def _ensure_audit_on_path():
    """Put ``security/`` on sys.path so ``import dependency_audit`` works when the
    tests are run from the ``preservation/`` cwd with ``--noconftest``."""
    if SECURITY_DIR not in sys.path:
        sys.path.insert(0, SECURITY_DIR)


def import_audit():
    """Import and return the REAL ``dependency_audit`` module (F4 gate)."""
    _ensure_audit_on_path()
    import dependency_audit  # noqa: E402

    return dependency_audit


# --------------------------------------------------------------------------- #
# In-scope / out-of-scope source paths (relative to REPO_ROOT).
# --------------------------------------------------------------------------- #
# The two maintained Python-3.11 pin sites (the ONLY files whose ``requests``
# token changes; sourced from the audit module in tests via SETUP_STATION_REL /
# BACKEND_REQS_REL).
SETUP_STATION_REL = os.path.join("station_install", "setup_station.sh")
BACKEND_REQS_REL = os.path.join("src", "backend", "requirements.txt")

# The host prereq patch script that carries the third UNPINNED Python-3.6
# ``--upgrade requests`` install (Req 3.1).
PATCH_DOCKER_HOST_REL = os.path.join("station_install", "patch_docker_host_prereqs.sh")

# Out-of-scope, byte-for-byte-unchanged references (Req 3.5, 3.6).
VENDORED_AUTH_REL = os.path.join(
    "edge-cv-portal", "backend", "layers", "shared", "python", "requests", "auth.py"
)
PORTAL_JWT_REQS_REL = os.path.join(
    "edge-cv-portal", "backend", "layers", "jwt", "requirements.txt"
)
PORTAL_FUNCTIONS_REQS_REL = os.path.join(
    "edge-cv-portal", "backend", "functions", "requirements.txt"
)
VENDORED_URLLIB3_DIR_REL = os.path.join(
    "edge-cv-portal", "backend", "layers", "shared", "python", "urllib3"
)

# The CVE-vulnerable and CVE-fixed version tokens (the ONLY allowed delta).
BASELINE_REQUESTS_VERSION = "2.32.3"  # F(X): unfixed pin (CVE-2024-47081)
FIXED_REQUESTS_VERSION = "2.32.4"  # F'(X): fixed pin


# --------------------------------------------------------------------------- #
# Location helpers -- find lines by CONTENT (never hardcoded line numbers, since
# a future edit above a site could drift them; the fix itself is token-only and
# adds/removes no line, but locating by content keeps the golden robust).
# --------------------------------------------------------------------------- #
def read_lines(rel_path):
    """Return the file split into lines (no trailing newline elements)."""
    return read_repo_file(rel_path).splitlines()


def find_line_index(rel_path, substring):
    """Return the 0-based index of the (unique) line containing ``substring``.

    Raises AssertionError if the substring is absent or appears more than once,
    so the golden always pins an unambiguous, content-located line."""
    lines = read_lines(rel_path)
    matches = [i for i, ln in enumerate(lines) if substring in ln]
    assert len(matches) == 1, (
        f"expected exactly one line containing {substring!r} in {rel_path}, "
        f"found {len(matches)} (lines {[i + 1 for i in matches]})"
    )
    return matches[0]


def located_line(rel_path, substring):
    """Return ``(lineno, text)`` (1-based lineno) for the unique line containing
    ``substring``."""
    idx = find_line_index(rel_path, substring)
    return idx + 1, read_lines(rel_path)[idx]


# --------------------------------------------------------------------------- #
# sha256 helpers (binary-exact).
# --------------------------------------------------------------------------- #
def sha256_file(rel_path):
    """Return the hex sha256 of a repo file read in binary (byte-exact)."""
    return hashlib.sha256(read_repo_file(rel_path, binary=True)).hexdigest()


def sha256_tree(rel_dir):
    """Return a deterministic manifest of a vendored package directory:
    ``{"file_count": N, "aggregate_sha256": hex, "files": {relpath: sha256}}``.

    Skips ``__pycache__`` dirs and ``*.pyc`` compiled artifacts (build outputs
    that are not part of the vendored source and can differ between runs)."""
    root = os.path.join(REPO_ROOT, rel_dir)
    per_file = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__")
        for name in sorted(filenames):
            if name.endswith(".pyc"):
                continue
            abs_path = os.path.join(dirpath, name)
            rel = os.path.relpath(abs_path, root).replace(os.sep, "/")
            with open(abs_path, "rb") as fh:
                per_file[rel] = hashlib.sha256(fh.read()).hexdigest()
    aggregate = hashlib.sha256()
    for rel in sorted(per_file):
        aggregate.update(rel.encode("utf-8"))
        aggregate.update(per_file[rel].encode("utf-8"))
    return {
        "file_count": len(per_file),
        "aggregate_sha256": aggregate.hexdigest(),
        "files": per_file,
    }


# --------------------------------------------------------------------------- #
# Golden capture-or-assert primitives (capture on first run when the baseline is
# absent, assert-equal thereafter).
# --------------------------------------------------------------------------- #
def baseline_path(name):
    return os.path.join(BASELINES, name)


def capture_or_assert_json(name, current):
    """Capture ``current`` to the baseline ``name`` (JSON) when absent (first run
    on the unfixed tree), else assert it still equals the recorded golden."""
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
    """Capture ``current_text`` to the baseline ``name`` when absent, else return
    the recorded golden (comparison is done by the caller, since the two pin-file
    goldens allow a single-token delta at the fixed pin line)."""
    path = baseline_path(name)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(current_text)
        return current_text
    with open(path, encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# Full-file pin-golden comparison: every line byte-for-byte identical EXCEPT the
# single in-scope ``requests==`` pin line, which may differ ONLY in its version
# token (``2.32.3`` on the unfixed tree -> ``2.32.4`` on the fixed tree).
# --------------------------------------------------------------------------- #
def _normalize_requests_token(line):
    """Replace any ``requests==<version>`` with a fixed placeholder so two lines
    that differ ONLY in the pinned version compare equal."""
    audit = import_audit()
    return audit._REQUESTS_PIN_RE.sub("requests==<VERSION>", line)


def assert_pin_file_matches_baseline(name, rel_path, pin_substring):
    """Capture-or-assert a full-file golden for an in-scope pin file.

    * On first run (unfixed tree) the raw file text is captured to ``name``.
    * Thereafter: assert the file has the same number of lines; every line is
      byte-for-byte identical to the golden EXCEPT the unique pin line (located
      by ``pin_substring``), which may differ ONLY in its ``requests==`` version
      token. Also assert the current pin version is either the baseline
      ``2.32.3`` or the fixed ``2.32.4`` (no other value is legitimate).

    Returns ``(golden_lines, current_lines, pin_index)`` for further assertions.
    """
    audit = import_audit()
    current_text = read_repo_file(rel_path)
    recorded_text = capture_or_assert_text(name, current_text)

    golden_lines = recorded_text.splitlines()
    current_lines = current_text.splitlines()
    assert len(current_lines) == len(golden_lines), (
        f"{rel_path}: line count changed ({len(current_lines)} vs golden "
        f"{len(golden_lines)}) -- preservation requires only a single version "
        f"token to change"
    )

    pin_idx = find_line_index(rel_path, pin_substring)

    for i, (cur, gold) in enumerate(zip(current_lines, golden_lines)):
        if i == pin_idx:
            assert _normalize_requests_token(cur) == _normalize_requests_token(gold), (
                f"{rel_path}:{i + 1} changed outside the requests version token.\n"
                f"  golden:  {gold!r}\n  current: {cur!r}"
            )
            m = audit._REQUESTS_PIN_RE.search(cur)
            assert m is not None, f"{rel_path}:{i + 1} lost its requests== pin"
            assert m.group(1) in (BASELINE_REQUESTS_VERSION, FIXED_REQUESTS_VERSION), (
                f"{rel_path}:{i + 1} pins requests=={m.group(1)}, expected "
                f"{BASELINE_REQUESTS_VERSION} (unfixed) or "
                f"{FIXED_REQUESTS_VERSION} (fixed)"
            )
        else:
            assert cur == gold, (
                f"{rel_path}:{i + 1} changed but only the requests version token "
                f"may differ.\n  golden:  {gold!r}\n  current: {cur!r}"
            )
    return golden_lines, current_lines, pin_idx
