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
"""Repo audit for the secrets / credentials / JWT bug-condition patterns
(security-secrets-credentials-jwt-fixes, finding S10 / Req 2.10).

This is the companion gate to the sibling ``repo_audit.py`` (Group 1). It owns a
DIFFERENT set of patterns (full-event logging, AWS-credential interpolation into
command strings, and an un-annotated ``verify_signature=False``) and a different
in-scope file set, so it is deliberately a separate module rather than an edit to
the sibling's already-green gate. To avoid duplication it REUSES the sibling's
proven low-level helpers (``REPO_ROOT``, ``EXCLUDE_DIRS``,
``EXCLUDED_PATH_SUBSTRING``, ``Hit``, ``_grep``, ``_parse_line``,
``_is_comment_line``) and defines only its own ``AUDIT_PATTERNS``,
``IN_SCOPE_FILES``, and precise ``_is_disallowed``.

Two layers that intentionally differ in breadth (mirroring ``repo_audit.py``):

* ``run_audit()`` -- the RAW, broad enumeration. It ``grep``s the whole in-scope
  tree (only ``cdk.out/asset.*`` excluded) for every bug-condition token, plus
  the enumeration-only S6-S9 markers (undocumented ``0.0.0.0`` bind, the empty
  pagination cursor, the bucket/secret name, the test-only token literals). On
  the UNFIXED tree it surfaces NON-EMPTY hits across S1/S2/S5 (the
  counterexamples that confirm the bug); task 1 uses it to list them. It applies
  NO precision filtering -- that is by design for the exploration phase.

* ``disallowed_hits()`` -- the PRECISE pattern gate re-run in task 7. It
  implements the exact design semantics below so that ONLY a genuinely
  un-neutralized occurrence in in-scope application code counts. The boto3 client
  kwargs ``aws_access_key_id=credentials.access_key`` (a bare keyword arg, no
  surrounding string literal), a comment/docstring mention, a documented
  ``# nosem``/``# nosec`` line, and any occurrence in an out-of-scope file are
  NOT disallowed. After the fix this must return zero (minus documented
  exceptions).

Precise gate semantics (from design "Repo-audit design" / "Precise gate
semantics"):
  * ``log_event_dump`` -- a logging call whose argument contains
    ``json.dumps(event``:
    ``logger\\.(info|debug|warning|error|critical)\\(.*json\\.dumps\\(\\s*event\\b``.
    Disallowed when present without a documented marker. After S1 this is gone.
  * ``cred_in_command`` -- ``\\.(access_key|secret_key)\\b`` (or
    ``access_key``/``secret_key`` inside ``{...}``) referenced INSIDE a string
    literal being built for a command -- detected as an f-string on the line
    (``\\b[rRbB]?f['\\"]``) or ``%`` / ``.format(`` / ``+`` interpolation. The
    boto3 client kwargs ``aws_access_key_id=credentials.access_key`` do NOT match
    and are allowed. After S2 the two ``export ...`` list entries and the
    ``-a``/``-s`` f-string fragment are gone.
  * ``unverified_jwt`` -- ``verify_signature['\\"]?\\s*[:=]\\s*False``. Disallowed
    when present WITHOUT a documented ``# nosem`` on the line. After S5 the
    pre-parse line carries the documented marker, so it is allowed.

In-scope scoping: the gate is asserted ONLY over ``IN_SCOPE_FILES`` (the six real
source paths this spec owns), so it does NOT match the security test/fixture
files' own pattern strings, the vendored ``src/backend/edgemlsdk/edgemlsdk/``
duplicate of ``deploy.py``, the generated ``cdk.out/asset.*`` artifacts, or files
owned by other specs. Precision + this scoping -- not a hard-coded line list --
is what lets the gate still FAIL if a full-event log, an interpolated credential,
or an un-annotated ``verify_signature=False`` is reintroduced into any real
in-scope source file.
"""
import os
import re

# Reuse the sibling module's proven low-level helpers where sensible. If the
# import is unavailable in some runner, fall back to a thin re-implementation so
# this module stays self-contained.
try:
    from repo_audit import (  # type: ignore
        REPO_ROOT,
        EXCLUDE_DIRS,
        EXCLUDED_PATH_SUBSTRING,
        Hit,
        _grep,
        _parse_line,
        _is_comment_line,
    )
except Exception:  # pragma: no cover - fallback when repo_audit is not importable
    import subprocess
    from collections import namedtuple

    REPO_ROOT = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
    )
    EXCLUDED_PATH_SUBSTRING = os.path.join("cdk.out", "asset.")
    EXCLUDE_DIRS = [
        ".git", "node_modules", ".hypothesis", "__pycache__", ".venv", "venv",
    ]
    Hit = namedtuple("Hit", ["category", "path", "lineno", "text"])

    def _grep(pattern):
        cmd = ["grep", "-rnE", "--include=*.py"]
        for d in EXCLUDE_DIRS:
            cmd.append("--exclude-dir=" + d)
        cmd.extend([pattern, REPO_ROOT])
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode not in (0, 1):
            raise RuntimeError(f"grep failed ({proc.returncode}): {proc.stderr}")
        return proc.stdout.splitlines()

    def _parse_line(category, raw):
        m = re.match(r"^(.*?):(\d+):(.*)$", raw)
        if not m:
            return None
        return Hit(category=category, path=m.group(1),
                   lineno=int(m.group(2)), text=m.group(3))

    def _is_comment_line(text):
        return text.lstrip().startswith("#")


# ---------------------------------------------------------------------------
# This spec's OWN audit patterns.
# ---------------------------------------------------------------------------
# The first three are the bug-condition SINKS that the precise gate enforces.
# The remaining ("enumeration-only") patterns surface the S6-S9 undocumented
# markers in the raw ``run_audit()`` report; the precise gate treats them as
# documented once S6-S9 land, so they are NOT part of ``disallowed_hits()``.
AUDIT_PATTERNS = [
    # S1 -- full API Gateway event dumped into a logging call.
    ("log_event_dump",
     r"logger\.(info|debug|warning|error|critical)\(.*json\.dumps\(\s*event\b"),
    # S2 -- AWS access/secret key referenced (precise filter narrows to the
    # ones interpolated into a command string, excluding boto3 kwargs).
    ("cred_in_command", r"\.(access_key|secret_key)\b"),
    # S5 -- unverified JWT decode.
    ("unverified_jwt", r"verify_signature['\"]?\s*[:=]\s*False"),
    # --- enumeration-only (S6-S9) -------------------------------------------
    ("bind_0000", r"host\s*=\s*['\"]0\.0\.0\.0['\"]"),
    ("empty_pagination_cursor", r"pagination_token\s*=\s*['\"]['\"]"),
    ("bucket_secret_name", r"edgeml-sdk-longevity-tests"),
    ("test_token_literal", r"token\s*=\s*['\"](good-token|inactive-token|some-token)"),
]

# The real source paths this spec owns (relative to REPO_ROOT), excluding
# vendored/generated copies. The precise gate is asserted only over these.
IN_SCOPE_FILES = frozenset(
    os.path.normpath(p)
    for p in (
        os.path.join("edge-cv-portal", "backend", "functions", "jwt_authorizer.py"),
        os.path.join("src", "edgemlsdk", "src", "test", "longevity", "deploy.py"),
        os.path.join("edge-cv-portal", "backend", "functions", "packaging.py"),
        os.path.join("src", "backend", "app.py"),
        os.path.join("edge-cv-portal", "backend", "functions", "components.py"),
        os.path.join("test", "backend-test", "utils", "test_auth.py"),
    )
)


def run_audit():
    """Return all raw audit Hits across the in-scope tree (``cdk.out/asset.*``
    excluded). No ``# nosem`` / scope filtering is applied here -- this is the
    raw bug-condition enumeration used by the exploration test."""
    hits = []
    for category, pattern in AUDIT_PATTERNS:
        for raw in _grep(pattern):
            if EXCLUDED_PATH_SUBSTRING in raw:
                continue
            hit = _parse_line(category, raw)
            if hit is None:
                continue
            if EXCLUDED_PATH_SUBSTRING in hit.path:
                continue
            hits.append(hit)
    return hits


def _has_nosem(text):
    """True if the source line carries a documented suppression marker
    (``# nosem`` / ``# nosec`` / ``# noqa``)."""
    low = text.lower()
    return "nosem" in low or "nosec" in low or "noqa" in low


def _is_in_scope(hit):
    """True if the hit is in application code this spec owns (see
    ``IN_SCOPE_FILES``). Uses the exact repo-relative path so the vendored
    ``src/backend/edgemlsdk/edgemlsdk/...`` copy does NOT match the real
    ``src/edgemlsdk/...`` source."""
    rel = os.path.normpath(os.path.relpath(hit.path, REPO_ROOT))
    return rel in IN_SCOPE_FILES


def _in_command_string(text):
    """True if the credential reference on this line sits inside a string
    literal being built for a command -- an f-string / ``%`` / ``.format(`` /
    ``+`` concatenation. A bare boto3 kwarg
    (``aws_access_key_id=credentials.access_key``) has none of these and is
    therefore allowed."""
    if re.search(r"\b[rRbB]?f['\"]", text):  # f-string / rf-string
        return True
    if ".format(" in text:
        return True
    if re.search(r"%[sdrfxeg%]", text) or re.search(r"['\"]\s*%\s*[\(\w]", text):
        return True
    if re.search(r"['\"]\s*\+", text) or re.search(r"\+\s*['\"]", text):
        return True
    # A brace-interpolation field around the credential -- {...access_key...} /
    # {...secret_key...}. This catches a multi-line f-string whose opening ``f"``
    # is on an earlier physical line (e.g. the mqtt run command) while still
    # NOT matching the bare boto3 kwargs ``aws_access_key_id=credentials.access_key``
    # (no surrounding braces).
    if re.search(r"\{[^{}]*(access_key|secret_key)[^{}]*\}", text):
        return True
    return False


def _is_disallowed(hit):
    """Apply the PRECISE design semantics to a single raw hit (for the three
    bug-condition sinks). Enumeration-only categories never count as
    disallowed."""
    # Documented, justified exceptions and pure-comment lines carry no live sink.
    if _has_nosem(hit.text):
        return False
    if _is_comment_line(hit.text):
        return False

    if hit.category == "log_event_dump":
        # A logging call dumping the whole event is always a sink.
        return True

    if hit.category == "cred_in_command":
        # Disallowed ONLY when the key is interpolated into a command string;
        # the bare boto3 client kwargs are allowed.
        return _in_command_string(hit.text)

    if hit.category == "unverified_jwt":
        # Disallowed when the unverified decode carries no documented marker
        # (the # nosem case is handled above).
        return True

    # Enumeration-only categories (bind_0000, B105/B106 literals) are surfaced
    # by run_audit() but are not part of the precise gate.
    return False


def disallowed_hits():
    """The PRECISE post-fix pattern gate (task 7).

    A raw ``run_audit()`` hit is *disallowed* only when ALL of the following
    hold:
      * it is in in-scope application code (``IN_SCOPE_FILES``) -- the security
        test/fixtures, the vendored ``edgemlsdk/edgemlsdk/`` duplicate, the
        generated ``cdk.out/asset.*`` artifacts, and other specs' files are out
        of scope;
      * it is not a comment line and carries no documented ``# nosem``/``# nosec``;
      * it matches the precise design semantics for one of the three sinks
        (full-event log; credential interpolated into a command string;
        un-annotated ``verify_signature=False``).

    On the UNFIXED tree this is non-empty (the S1/S2/S5 counterexamples); after
    the fix it must be empty (minus documented exceptions)."""
    return [
        hit for hit in run_audit()
        if _is_in_scope(hit) and _is_disallowed(hit)
    ]


def hits_for(path_substring, hits=None):
    """All hits whose file path contains ``path_substring``."""
    hits = run_audit() if hits is None else hits
    return [h for h in hits if path_substring in h.path]


# The in-scope findings -> a path fragment that uniquely identifies the real
# source file (NOT the generated cdk.out/asset.* copies or the vendored
# edgemlsdk/edgemlsdk/ duplicate).
IN_SCOPE_SITES = {
    "S1 jwt_authorizer full-event log": os.path.join(
        "edge-cv-portal", "backend", "functions", "jwt_authorizer.py"),
    "S2 deploy.py credential interpolation": os.path.join(
        "src", "edgemlsdk", "src", "test", "longevity", "deploy.py"),
    "S3 packaging.py synthetic email": os.path.join(
        "edge-cv-portal", "backend", "functions", "packaging.py"),
    "S6 app.py 0.0.0.0 bind": os.path.join("src", "backend", "app.py"),
    "S7 components.py pagination cursor": os.path.join(
        "edge-cv-portal", "backend", "functions", "components.py"),
    "S9 test_auth.py token fixtures": os.path.join(
        "test", "backend-test", "utils", "test_auth.py"),
}


if __name__ == "__main__":
    all_hits = run_audit()
    print(f"Secrets audit: {len(all_hits)} raw bug-condition hits "
          f"(cdk.out/asset.* excluded)\n")
    for label, frag in IN_SCOPE_SITES.items():
        site_hits = hits_for(frag, all_hits)
        print(f"=== {label} ({frag}) : {len(site_hits)} raw hit(s) ===")
        for h in site_hits:
            print(f"  [{h.category}] {h.path}:{h.lineno}: {h.text.strip()}")
        print()

    disallowed = disallowed_hits()
    print("-" * 70)
    print(f"PATTERN GATE: {len(disallowed)} disallowed hit(s) in in-scope "
          f"application code (must be 0, minus documented # nosem/# nosec).")
    for h in disallowed:
        rel = os.path.relpath(h.path, REPO_ROOT)
        print(f"  [{h.category}] {rel}:{h.lineno}: {h.text.strip()}")
    raise SystemExit(1 if disallowed else 0)
