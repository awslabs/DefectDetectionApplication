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
"""Repo audit for the dependency / supply-chain CVE & weak-hash bug-condition
patterns (security-dependency-cve-fixes, finding F4 / Req 2.4).

This is the companion gate to the sibling ``repo_audit.py`` (Group 1 --
injection / deserialization), ``secrets_audit.py`` (Group 3 -- secrets / JWT),
``iam_audit.py`` (Group 4 -- IAM / authorization), ``s3_squat_audit.py``
(Group 5 -- S3 bucket squatting), and ``docker_base_image_audit.py`` (Group 6 --
Docker non-ECR base images). It owns a DIFFERENT set of patterns (in-scope
``requests==`` pins on the two maintained Python-3.11 pin files, plus the B324
vendored HTTP Digest-auth allowlist) and a different in-scope file set, so it is
deliberately a separate module rather than an edit to the siblings' already-green
gates. To avoid duplication it REUSES the siblings' proven low-level primitives
(``REPO_ROOT``, ``EXCLUDED_PATH_SUBSTRING``, ``Hit``) via the SAME try/except
fallback re-implementation the siblings use when ``repo_audit`` is not importable,
and defines only its OWN constants, the pin classification, and the precise
``disallowed_hits()`` logic. (A string-based ``_has_nosem`` is re-implemented
locally because the sibling ``repo_audit._has_nosem`` keys on a ``Hit`` object,
whereas this gate classifies a bare pin *line* string.)

Two layers that intentionally differ in breadth (mirroring the siblings):

* ``run_audit()`` -- the RAW, broad, line-based enumeration. It scans the two
  in-scope pin files for every ``requests==<version>`` pin and emits a ``Hit``
  per pin. It applies NO precision / classification filtering beyond
  ``IN_SCOPE_PIN_FILES`` scoping -- that is by design for the exploration phase.
  On the UNFIXED tree it surfaces the two in-scope ``requests==2.32.3`` pins
  (F1, F2 -- the counterexamples that confirm the bug); task 1 uses it to list
  them. It NEVER parses the unpinned system-``python3`` (3.6) installs (no
  ``==``), the portal ``requests==2.31.0`` pins, the ``urllib3`` pins, or
  ``cdk.out/**``.

* ``disallowed_hits()`` -- the PRECISE per-pin post-fix gate re-run in task 6.
  For each ``requests==<version>`` pin in the two in-scope files it keys on the
  parsed version: a pin is *disallowed* iff ``_pin_is_disallowed(version)``
  (``< MIN_SAFE_REQUESTS`` = ``2.32.4``, i.e. CVE-2024-47081) AND the line
  carries no ``# nosec`` marker. A pin ``>= 2.32.4`` is cleared; a ``# nosec``
  marker clears the line; a BARE unpinned ``requests`` (no ``==``) never matches
  the regex, so it is never flagged (the Python-3.6 host installs stay clear).
  On the UNFIXED tree this returns the two ``2.32.3`` pins; after the bump it
  must be empty.

B324 documented allowlist: ``ACCEPTED_EXCEPTIONS`` records the three known
vendored ``requests/auth.py`` HTTP Digest-auth ``md5`` / ``sha1`` lines
(148/156/205) as reviewed-and-accepted false positives (RFC 2617 / RFC 7616
protocol-mandated hashing, not security hashing). ``verify_accepted_exceptions_
still_match()`` is a SPECIFICITY guard: it returns the exception entries whose
recorded ``(path, lineno, token)`` no longer match the vendored file, so an empty
list means the allowlist still describes exactly the known protocol usages and a
NEW / moved weak hash elsewhere is NOT silently covered.
"""
import os
import re
from collections import namedtuple

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
    REPO_ROOT = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
    )
    EXCLUDED_PATH_SUBSTRING = os.path.join("cdk.out", "asset.")
    Hit = namedtuple("Hit", ["category", "path", "lineno", "text"])


# ---------------------------------------------------------------------------
# In-scope pin files (relative to REPO_ROOT, normpath'd) -- ONLY the two
# maintained Python-3.11 pin files this spec owns. The gate parses
# ``requests==`` pins from THESE TWO FILES ONLY -- never the unpinned
# system-``python3`` (3.6) installs, the portal ``requests==2.31.0`` pins, the
# ``urllib3`` pins, or ``cdk.out/**``.
# ---------------------------------------------------------------------------
SETUP_STATION_REL = os.path.normpath(
    os.path.join("station_install", "setup_station.sh")
)  # F1
BACKEND_REQS_REL = os.path.normpath(
    os.path.join("src", "backend", "requirements.txt")
)  # F2

IN_SCOPE_PIN_FILES = frozenset((SETUP_STATION_REL, BACKEND_REQS_REL))

# ---------------------------------------------------------------------------
# Disallowed-pin predicate. CVE-2024-47081 (.netrc credential leak, CVSS 5.3)
# is fixed in requests 2.32.4, so any in-scope pin < 2.32.4 is disallowed.
# ---------------------------------------------------------------------------
MIN_SAFE_REQUESTS = (2, 32, 4)
_REQUESTS_PIN_RE = re.compile(r"\brequests==([0-9]+(?:\.[0-9]+)*)\b")


def _parse_version(v):
    """Parse a dotted version string into a normalized 3-int tuple
    ``(major, minor, patch)`` (missing components pad to 0, extra components are
    truncated). Non-numeric components fall back to 0 so a malformed pin does not
    crash the gate."""
    parts = []
    for component in str(v).split("."):
        try:
            parts.append(int(component))
        except ValueError:
            parts.append(0)
    parts = (parts + [0, 0, 0])[:3]
    return tuple(parts)


def _pin_is_disallowed(v):
    """True iff the parsed ``requests`` version is below the CVE-fixed floor
    (``< 2.32.4``)."""
    return _parse_version(v) < MIN_SAFE_REQUESTS


# ---------------------------------------------------------------------------
# B324 documented accepted false positives (vendored HTTP Digest-auth).
# ---------------------------------------------------------------------------
AcceptedException = namedtuple(
    "AcceptedException", ["path", "lineno", "token", "justification"]
)

# Accepted false positives (documented, reviewed). B324 (hashlib.md5 /
# hashlib.sha1 without usedforsecurity=False) on the VENDORED requests HTTP
# Digest-auth implementation. These md5/sha1 calls implement RFC 2617 / RFC 7616
# HTTP Digest authentication (the MD5 / SHA / MD5-SESS Digest schemes), where the
# hash algorithm is PROTOCOL-MANDATED, not a DDA data-integrity/security hash.
# The file is VENDORED (regenerated by edge-cv-portal/backend/layers/shared/
# build.sh from requirements.txt), so a hand-edit would not survive a rebuild,
# and upgrading requests does not remove the calls. Reviewed and accepted as a
# false positive. Scoped to the three KNOWN lines only.
VENDORED_DIGEST_AUTH_REL = os.path.join(
    "edge-cv-portal", "backend", "layers", "shared", "python", "requests", "auth.py"
)
ACCEPTED_EXCEPTIONS = (
    AcceptedException(
        path=VENDORED_DIGEST_AUTH_REL,
        lineno=148,
        token="hashlib.md5",
        justification=(
            "RFC 2617/7616 HTTP Digest MD5 scheme (md5_utf8); protocol-mandated "
            "digest hashing, not DDA security/data-integrity hashing"
        ),
    ),
    AcceptedException(
        path=VENDORED_DIGEST_AUTH_REL,
        lineno=156,
        token="hashlib.sha1",
        justification=(
            "RFC 2617/7616 HTTP Digest SHA scheme (sha_utf8); protocol-mandated "
            "digest hashing, not DDA security/data-integrity hashing"
        ),
    ),
    AcceptedException(
        path=VENDORED_DIGEST_AUTH_REL,
        lineno=205,
        token="hashlib.sha1",
        justification=(
            "RFC 2617/7616 HTTP Digest cnonce derivation; protocol-mandated "
            "digest hashing, not DDA security/data-integrity hashing"
        ),
    ),
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
    """True if a path is a generated cdk.out artifact (defensive; the in-scope
    pin files are never under cdk.out)."""
    return EXCLUDED_PATH_SUBSTRING in rel_path or "cdk.out" in rel_path


# ---------------------------------------------------------------------------
# Layer 1 -- raw, broad, line-based enumeration of every in-scope requests pin.
# ---------------------------------------------------------------------------
def run_audit():
    """Return a Hit per ``requests==<version>`` pin across the two in-scope pin
    files (the unpinned Python-3.6 installs, the portal pins, ``urllib3``, and
    cdk.out are never parsed). No classification / exception filtering is applied
    -- this is the raw bug-condition enumeration used by the exploration test.
    Non-empty on the unfixed tree (the two ``requests==2.32.3`` pins)."""
    hits = []
    for rel_path in sorted(IN_SCOPE_PIN_FILES):
        if _excluded_path(rel_path):
            continue
        abs_path = os.path.join(REPO_ROOT, rel_path)
        text = _read(rel_path)
        if not text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for m in _REQUESTS_PIN_RE.finditer(line):
                version = m.group(1)
                hits.append(Hit(category="requests_pin", path=abs_path,
                                lineno=lineno, text=line))
    return hits


# ---------------------------------------------------------------------------
# Layer 2 -- precise per-pin post-fix gate.
# ---------------------------------------------------------------------------
def disallowed_hits():
    """The PRECISE post-fix per-pin gate (task 6).

    A ``requests==<version>`` pin is *disallowed* only when it is in an in-scope
    pin file, its parsed version is ``< MIN_SAFE_REQUESTS`` (2.32.4 --
    CVE-2024-47081), and the line carries no documented ``# nosec`` exception. A
    BARE unpinned ``requests`` (no ``==``) never matches the regex, so it is
    never flagged (the Python-3.6 host installs + ``--upgrade requests`` lines
    stay clear).

    On the UNFIXED tree this is non-empty (the two ``requests==2.32.3``
    counterexamples); after the bump it must be empty."""
    hits = []
    for rel_path in sorted(IN_SCOPE_PIN_FILES):
        if _excluded_path(rel_path):
            continue
        abs_path = os.path.join(REPO_ROOT, rel_path)
        text = _read(rel_path)
        if not text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            if _has_nosem(line):
                continue
            for m in _REQUESTS_PIN_RE.finditer(line):
                version = m.group(1)
                if _pin_is_disallowed(version):
                    hits.append(Hit(
                        "vulnerable_requests_pin", abs_path, lineno,
                        f"in-scope pin 'requests=={version}' is < "
                        f"{'.'.join(map(str, MIN_SAFE_REQUESTS))} "
                        f"(CVE-2024-47081): {line.strip()}"))
    return hits


def verify_accepted_exceptions_still_match():
    """SPECIFICITY guard for the B324 allowlist.

    Return the ``ACCEPTED_EXCEPTIONS`` entries whose recorded
    ``(path, lineno, token)`` no longer match the vendored ``auth.py`` (the token
    is no longer present on the recorded line). An empty list means the allowlist
    still describes exactly the known protocol usages, so a NEW / moved weak hash
    elsewhere is NOT silently covered by this exception."""
    drifted = []
    file_cache = {}
    for exc in ACCEPTED_EXCEPTIONS:
        if exc.path not in file_cache:
            file_cache[exc.path] = _read(exc.path).splitlines()
        lines = file_cache[exc.path]
        if exc.lineno < 1 or exc.lineno > len(lines):
            drifted.append(exc)
            continue
        recorded_line = lines[exc.lineno - 1]
        if exc.token not in recorded_line:
            drifted.append(exc)
    return drifted


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
    print(f"Dependency/supply-chain CVE audit: {len(all_hits)} raw "
          f"requests== pin(s) in the two in-scope pin files "
          f"(unpinned py3.6 installs, portal pins, urllib3 & cdk.out excluded)\n")
    for h in all_hits:
        print(f"  [{h.category}] {_rel(h.path)}:{h.lineno}: {h.text.strip()}")
    print()

    print("Documented B324 accepted false positives (vendored digest-auth):")
    for exc in ACCEPTED_EXCEPTIONS:
        print(f"  {exc.path}:{exc.lineno} {exc.token} -- {exc.justification}")
    drifted = verify_accepted_exceptions_still_match()
    if drifted:
        print("\n  ** ALLOWLIST DRIFT ** the following recorded exceptions no "
              "longer match the vendored file:")
        for exc in drifted:
            print(f"    {exc.path}:{exc.lineno} expected token {exc.token!r}")
    print()

    disallowed = disallowed_hits()
    grouped = disallowed_by_category(disallowed)
    print("-" * 70)
    print(f"PATTERN GATE: {len(disallowed)} disallowed requests pin(s) in "
          f"in-scope files (must be 0 after fix, minus documented exceptions).")
    for category in sorted(grouped):
        print(f"  --- {category}: {len(grouped[category])} hit(s) ---")
        for h in grouped[category]:
            print(f"    {_rel(h.path)}:{h.lineno}: {h.text.strip()}")
    raise SystemExit(1 if (disallowed or drifted) else 0)
