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
"""Repo audit for the S3 bucket-squatting bug-condition patterns
(security-s3-bucket-squatting-fixes, finding B7 / Req 2.7).

This is the companion gate to the sibling ``repo_audit.py`` (Group 1 --
injection / deserialization), ``secrets_audit.py`` (Group 3 -- secrets / JWT),
and ``iam_audit.py`` (Group 4 -- IAM / authorization). It owns a DIFFERENT set
of patterns (predictable S3 bucket literals read-from / written-to / shown in
copy-pasteable docs / notebook code with no adjacent ``head-bucket``
owner-assertion preflight, env-var parameterization, or placeholder) and a
different in-scope file set, so it is deliberately a separate module rather than
an edit to the siblings' already-green gates. To avoid duplication it REUSES the
siblings' proven low-level primitives (``REPO_ROOT``, ``EXCLUDE_DIRS``,
``EXCLUDED_PATH_SUBSTRING``, ``Hit``, ``_parse_line``, ``_is_comment_line``,
with the same try/except fallback re-implementation ``iam_audit.py`` uses when
``repo_audit`` is not importable) and defines only its OWN ``AUDIT_PATTERNS``,
``IN_SCOPE_FILES``, ``PREDICTABLE_BUCKETS``, and precise ``_is_disallowed``.

Two layers that intentionally differ in breadth (mirroring the siblings):

* ``run_audit()`` -- the RAW, broad, line-based enumeration. It scans the five
  in-scope source files for every bug-condition token (a predictable bucket on
  an ``aws s3 cp``/``sync`` line, an ``s3://<predictable>`` URI, a config
  ``"bucket": "<predictable>"`` value, plus the ``owner_assertion`` /
  ``placeholder`` clearing tokens). It applies NO precision / scoping filtering
  -- that is by design for the exploration phase. On the UNFIXED tree it surfaces
  NON-EMPTY hits across all six sites (B1-B6, the counterexamples that confirm
  the bug); task 1 uses it to list them.

* ``disallowed_hits()`` -- the PRECISE post-fix gate re-run in task 7. It parses
  ``deploy.py``'s SSM list (via ``ast``) and ``publish.sh`` structurally enough
  to associate a ``head-bucket`` preflight with the access it guards (the
  NEAREST PRECEDING preflight for the SAME bucket in the same list/script), not a
  file-global presence check, so dropping the preflight for one bucket while
  keeping another's still fails the gate. Documented exceptions (a ``# nosec`` /
  ``// nosec`` marker), placeholders (``<your-bucket-name>``), and the
  single-source ``sample_data_bucket`` notebook variable are NOT disallowed.
  After the fix this must return zero.

Precise gate semantics (from design "Repo-audit design" / "Precise gate
semantics"):
  * ``unverified_s3_access`` -- an ``aws s3 cp``/``sync`` against a predictable
    literal, OR an ``s3://<predictable>`` download URI, whose SAME LOGICAL BLOCK
    (the preceding list entry / statement for shell / SSM lists) has no
    ``--expected-bucket-owner`` / ``head-bucket`` preflight for that SAME bucket.
  * ``unverified_config_reference`` -- a ``"bucket": "<predictable>"`` config
    value (B5-shape) or a notebook download prefix bound to a bare predictable
    literal (B6-shape) that is not a ``<...>`` placeholder and is not derived
    from a documented single-source ``sample_data_bucket`` variable.
  * ``undocumented_doc_command`` -- an ``index.rst`` ``aws s3 cp`` example
    against a predictable bucket in a ``code-block`` with no preceding ownership
    ``.. note::`` and no documented ``head-bucket`` preflight in the same block.

In-scope scoping: the gate is asserted ONLY over ``IN_SCOPE_FILES`` (the five
real source paths this spec owns), so it does NOT match the vendored
``src/backend/edgemlsdk/edgemlsdk/...`` duplicate (also excluded defensively via
an ``os.path.join("edgemlsdk", "edgemlsdk")`` substring check), the generated
``cdk.out`` artifacts, or the security test/fixture files' own pattern strings.
"""
import ast
import json
import os
import re

# Reuse the sibling module's proven low-level primitives where sensible. If the
# import is unavailable in some runner, fall back to a thin re-implementation so
# this module stays self-contained.
try:
    from repo_audit import (  # type: ignore
        REPO_ROOT,
        EXCLUDE_DIRS,
        EXCLUDED_PATH_SUBSTRING,
        Hit,
        _parse_line,
        _is_comment_line,
    )
except Exception:  # pragma: no cover - fallback when repo_audit is not importable
    from collections import namedtuple

    REPO_ROOT = os.path.normpath(
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
    )
    EXCLUDED_PATH_SUBSTRING = os.path.join("cdk.out", "asset.")
    EXCLUDE_DIRS = [
        ".git", "node_modules", ".hypothesis", "__pycache__", ".venv", "venv",
    ]
    Hit = namedtuple("Hit", ["category", "path", "lineno", "text"])

    def _parse_line(category, raw):
        m = re.match(r"^(.*?):(\d+):(.*)$", raw)
        if not m:
            return None
        return Hit(category=category, path=m.group(1),
                   lineno=int(m.group(2)), text=m.group(3))

    def _is_comment_line(text):
        return text.lstrip().startswith("#")


# Defensive substring identifying the vendored / generated duplicate SDK subtree
# (src/backend/edgemlsdk/edgemlsdk/...). It regenerates from the maintained
# source at src/edgemlsdk/src/... and is explicitly out of scope.
VENDORED_DUP_SUBSTRING = os.path.join("edgemlsdk", "edgemlsdk")

# ---------------------------------------------------------------------------
# This spec's OWN predictable bucket literals. The last entry is a PREFIX match
# (lookoutvision-us-east-1-0e205be246 and any other lookoutvision-* sample
# bucket).
# ---------------------------------------------------------------------------
PREDICTABLE_BUCKETS = (
    "panorama-sdk-v2-artifacts",
    "edgeml-sdk-docs",
    "edgeml-sdk-longevity-tests",
    "lookoutvision-",  # prefix match
)

_BUCKET_ALTERNATION = "|".join(re.escape(b) for b in PREDICTABLE_BUCKETS)

# ---------------------------------------------------------------------------
# In-scope files (relative to REPO_ROOT) -- the five real source paths this spec
# owns (publish.sh covers both B2 and B3), excluding the vendored duplicate and
# cdk.out.
# ---------------------------------------------------------------------------
DEPLOY_REL = os.path.join("src", "edgemlsdk", "src", "test", "longevity", "deploy.py")
PUBLISH_REL = os.path.join("src", "edgemlsdk", "src", "utilities", "publish.sh")
INDEX_RST_REL = os.path.join("src", "edgemlsdk", "src", "docs", "source", "index.rst")
S3_RST_REL = os.path.join(
    "src", "edgemlsdk", "src", "docs", "source", "components", "message_broker", "s3.rst"
)
NOTEBOOK_REL = "DDA_SageMaker_Model_Training_and_Compilation.ipynb"

IN_SCOPE_FILES = frozenset(
    os.path.normpath(p)
    for p in (DEPLOY_REL, PUBLISH_REL, INDEX_RST_REL, S3_RST_REL, NOTEBOOK_REL)
)

# The six in-scope findings -> a path fragment that uniquely identifies the real
# source file (NOT the vendored edgemlsdk/edgemlsdk/... copies).
IN_SCOPE_SITES = {
    "B1 deploy.py SSM download list": DEPLOY_REL,
    "B2 publish.sh .deb/.whl uploads": PUBLISH_REL,
    "B3 publish.sh docs sync": PUBLISH_REL,
    "B4 index.rst install commands": INDEX_RST_REL,
    "B5 s3.rst message-broker config": S3_RST_REL,
    "B6 notebook old_prefix literal": NOTEBOOK_REL,
}

# ---------------------------------------------------------------------------
# RAW enumeration patterns (line-based, applied to every in-scope file). These
# are deliberately broad -- the raw run_audit() layer surfaces every token so
# the exploration test can list the counterexamples per finding.
# ---------------------------------------------------------------------------
AUDIT_PATTERNS = [
    # a predictable bucket literal on an aws s3 cp/sync line
    ("s3_cli_predictable_bucket",
     r"aws\s+s3\s+(?:cp|sync)\b.*(?:" + _BUCKET_ALTERNATION + r")"),
    # a predictable bucket literal in an s3:// URI (config / notebook / docs)
    ("s3_uri_predictable_bucket", r"s3://(?:" + _BUCKET_ALTERNATION + r")"),
    # a predictable bucket literal as a config "bucket" value
    ("config_predictable_bucket",
     r"\"bucket\"\s*:\s*\"(?:" + _BUCKET_ALTERNATION + r")"),
    # an owner assertion / preflight token (used to CLEAR a nearby access)
    ("owner_assertion", r"--expected-bucket-owner|head-bucket"),
    # a placeholder token (used to CLEAR a doc/config reference)
    ("placeholder", r"<your-bucket-name>|<PANORAMA_SDK_ACCOUNT>|sample_data_bucket"),
]

# Owner-assertion / preflight token used to CLEAR a nearby access.
_OWNER_ASSERTION_RE = re.compile(r"--expected-bucket-owner|head-bucket")
# An aws s3 cp/sync command line.
_S3_CLI_RE = re.compile(r"aws\s+s3\s+(?:cp|sync)\b")


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
    """True if the text carries a documented suppression marker."""
    low = text.lower()
    return "nosem" in low or "nosec" in low or "noqa" in low


def _excluded_path(rel_path):
    """True if a path is one of the out-of-scope trees (the vendored duplicate
    or a cdk.out artifact)."""
    return VENDORED_DUP_SUBSTRING in rel_path or EXCLUDED_PATH_SUBSTRING in rel_path


def _predictable_bucket_of(text):
    """Return the predictable-bucket identity referenced on this line, or None.

    The lookoutvision- prefix collapses to the literal "lookoutvision-" identity
    so all lookoutvision-* sample buckets associate to the same preflight."""
    for bucket in PREDICTABLE_BUCKETS:
        if bucket in text:
            return bucket
    return None


# ---------------------------------------------------------------------------
# Layer 1 -- raw, broad, line-based enumeration.
# ---------------------------------------------------------------------------
def run_audit():
    """Return all raw audit Hits across the five in-scope files (the vendored
    duplicate and cdk.out never scanned). No scoping / exception filtering is
    applied -- this is the raw bug-condition enumeration used by the exploration
    test. Non-empty on the unfixed tree across all six sites (B1-B6)."""
    hits = []
    for rel_path in sorted(IN_SCOPE_FILES):
        if _excluded_path(rel_path):
            continue
        abs_path = os.path.join(REPO_ROOT, rel_path)
        text = _read(rel_path)
        if not text:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for category, pattern in AUDIT_PATTERNS:
                if re.search(pattern, line):
                    hits.append(Hit(category=category, path=abs_path,
                                    lineno=lineno, text=line))
    return hits


# ---------------------------------------------------------------------------
# deploy.py SSM-list extraction (ast-based).
# ---------------------------------------------------------------------------
def _joinedstr_to_text(node):
    """Reconstruct an f-string node into a representative string: literal parts
    verbatim, interpolated {expr} parts as a PLACEHOLDER token. Enough to detect
    the s3:// literal / head-bucket --bucket <name> tokens the gate keys on."""
    parts = []
    for value in node.values:
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            parts.append(value.value)
        else:
            parts.append("PLACEHOLDER")
    return "".join(parts)


def _element_to_text(node):
    """Reconstruct a list element (a str Constant or an f-string) into text."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return _joinedstr_to_text(node)
    return ""


def extract_ssm_list(text, list_name="download_edgemlsdk_release_artifacts"):
    """Return the ordered list of command strings assigned to ``list_name`` in
    deploy.py, reconstructed from the AST (f-string interpolations become
    PLACEHOLDER). Returns [] if the module does not parse or the name is absent.

    This is the structural parse the per-bucket preflight association relies on:
    a head-bucket entry only clears the s3 accesses for the SAME bucket that
    FOLLOW it in the list."""
    try:
        tree = ast.parse(text)
    except SyntaxError:  # pragma: no cover
        return []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        targets = [t for t in node.targets if isinstance(t, ast.Name)]
        if not any(t.id == list_name for t in targets):
            continue
        if isinstance(node.value, (ast.List, ast.Tuple)):
            return [_element_to_text(el) for el in node.value.elts]
    return []


def _block_unverified_accesses(entries):
    """Given an ORDERED list of shell/SSM command strings, return the entries
    that are an aws s3 cp/sync (or an s3://<predictable> download) against a
    predictable bucket whose SAME BLOCK has no PRECEDING head-bucket preflight
    for that SAME bucket.

    Preflight association is per-bucket and order-sensitive: a
    ``head-bucket --bucket <B>`` entry only clears predictable accesses to
    bucket ``<B>`` that appear AFTER it, so dropping the preflight for one bucket
    while keeping another's still surfaces the unguarded access."""
    verified_buckets = set()
    unverified = []
    for idx, entry in enumerate(entries):
        if _has_nosem(entry):
            # A documented, justified exception clears the entry.
            continue
        # A head-bucket preflight marks its bucket verified for the entries below.
        if "head-bucket" in entry and "--expected-bucket-owner" in entry:
            bucket = _predictable_bucket_of(entry)
            if bucket:
                verified_buckets.add(bucket)
            continue
        # An s3 access line: is it against a predictable bucket?
        is_s3_cli = bool(_S3_CLI_RE.search(entry))
        has_s3_uri = "s3://" in entry
        if not (is_s3_cli or has_s3_uri):
            continue
        bucket = _predictable_bucket_of(entry)
        if bucket is None:
            continue
        if bucket not in verified_buckets:
            unverified.append((idx, bucket, entry))
    return unverified


# ---------------------------------------------------------------------------
# publish.sh structural parsing (env-var defaults + per-bucket preflight).
# ---------------------------------------------------------------------------
def _shell_var_defaults(text):
    """Parse ``VAR="${VAR:-default}"`` (and ``VAR=${VAR:-default}``) assignments
    into a {VAR: default} map so ``s3://${ARTIFACT_BUCKET}/...`` and
    ``head-bucket --bucket "$ARTIFACT_BUCKET"`` resolve to the same bucket
    identity for the per-bucket preflight association."""
    defaults = {}
    for m in re.finditer(
        r"(\w+)\s*=\s*\"?\$\{\s*\1\s*:-\s*([^}\"]+)\}\"?", text
    ):
        defaults[m.group(1)] = m.group(2).strip()
    return defaults


def _resolve_shell_bucket(text, defaults):
    """Return the predictable-bucket identity a shell line targets, resolving a
    ``${VAR}`` / ``$VAR`` reference through the parsed env-var defaults. Returns
    None if no predictable bucket is involved."""
    # Direct predictable literal on the line.
    direct = _predictable_bucket_of(text)
    if direct:
        return direct
    # A ${VAR} / $VAR reference whose default is a predictable literal.
    for m in re.finditer(r"\$\{?(\w+)\}?", text):
        var = m.group(1)
        default = defaults.get(var)
        if default:
            resolved = _predictable_bucket_of(default)
            if resolved:
                return resolved
    return None


def _publish_unverified_accesses(text):
    """Return the (lineno, bucket, line) tuples in publish.sh that are an
    ``aws s3 cp``/``sync`` against a predictable bucket (directly or via an env
    var defaulting to one) with no PRECEDING ``head-bucket`` preflight for that
    SAME bucket earlier in the script."""
    defaults = _shell_var_defaults(text)
    verified_buckets = set()
    unverified = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _is_comment_line(line) or _has_nosem(line):
            # Track preflights even on documented lines, but never flag them.
            if "head-bucket" in line and "--expected-bucket-owner" in line:
                bucket = _resolve_shell_bucket(line, defaults)
                if bucket:
                    verified_buckets.add(bucket)
            continue
        if "head-bucket" in line and "--expected-bucket-owner" in line:
            bucket = _resolve_shell_bucket(line, defaults)
            if bucket:
                verified_buckets.add(bucket)
            continue
        if not _S3_CLI_RE.search(line):
            continue
        bucket = _resolve_shell_bucket(line, defaults)
        if bucket is None:
            continue
        if bucket not in verified_buckets:
            unverified.append((lineno, bucket, line))
    return unverified


# ---------------------------------------------------------------------------
# index.rst code-block parsing (documented ownership note / preflight).
# ---------------------------------------------------------------------------
def _iter_rst_code_blocks(text):
    """Yield (start_line, block_lines, preceding_lines) for each
    ``.. code-block::`` directive in an rst file. ``block_lines`` are the
    indented body lines; ``preceding_lines`` are the ~10 lines before the
    directive (where an ownership ``.. note::`` would sit)."""
    lines = text.splitlines()
    i = 0
    n = len(lines)
    directive_re = re.compile(r"^(\s*)\.\.\s+code-block::")
    while i < n:
        m = directive_re.match(lines[i])
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        start_line = i + 1
        preceding = lines[max(0, i - 10):i]
        # Collect the body: subsequent blank lines or lines indented deeper than
        # the directive, until a non-blank line at <= the directive indentation.
        body = []
        j = i + 1
        while j < n:
            ln = lines[j]
            if ln.strip() == "":
                body.append(ln)
                j += 1
                continue
            cur_indent = len(ln) - len(ln.lstrip())
            if cur_indent > indent:
                body.append(ln)
                j += 1
                continue
            break
        yield start_line, body, preceding
        i = j


def _rst_undocumented_doc_commands(text):
    """Return (lineno, line) tuples for each index.rst ``aws s3 cp`` example
    against a predictable bucket in a code-block that has NO preceding ownership
    ``.. note::`` and NO documented ``head-bucket`` preflight in the same block."""
    hits = []
    for start_line, body, preceding in _iter_rst_code_blocks(text):
        block_text = "\n".join(body)
        cp_lines = [
            (idx, ln) for idx, ln in enumerate(body)
            if _S3_CLI_RE.search(ln) and _predictable_bucket_of(ln)
        ]
        if not cp_lines:
            continue
        note_before = any(".. note::" in pl for pl in preceding)
        preflight_in_block = (
            "head-bucket" in block_text and "--expected-bucket-owner" in block_text
        )
        if note_before or preflight_in_block:
            continue
        for idx, ln in cp_lines:
            hits.append((start_line + 1 + idx, ln.strip()))
    return hits


# ---------------------------------------------------------------------------
# s3.rst config-sample parsing (B5).
# ---------------------------------------------------------------------------
def _rst_unverified_config_refs(text):
    """Return (lineno, line) tuples for each ``"bucket": "<predictable>"`` config
    value in s3.rst that is a bare predictable literal (not a ``<...>``
    placeholder)."""
    hits = []
    config_re = re.compile(
        r"\"bucket\"\s*:\s*\"(" + _BUCKET_ALTERNATION + r")[^\"]*\""
    )
    for lineno, line in enumerate(text.splitlines(), start=1):
        if _has_nosem(line):
            continue
        if config_re.search(line):
            # A placeholder value (<...>) would not match the predictable
            # alternation, so any match here is a bare predictable literal.
            hits.append((lineno, line.strip()))
    return hits


# ---------------------------------------------------------------------------
# Notebook parsing (B6).
# ---------------------------------------------------------------------------
def _notebook_source_lines(text, cell_id="seg_manifest"):
    """Return (source_str, cell_index, prev_cell) for the notebook cell with the
    given id, parsed via json.load. Returns ("", -1, None) if absent."""
    try:
        nb = json.loads(text)
    except (ValueError, json.JSONDecodeError):  # pragma: no cover
        return "", -1, None
    cells = nb.get("cells", [])
    for idx, cell in enumerate(cells):
        if cell.get("id") == cell_id:
            src = cell.get("source", [])
            if isinstance(src, list):
                src = "".join(src)
            prev = cells[idx - 1] if idx > 0 else None
            return src, idx, prev
    return "", -1, None


def _notebook_unverified_refs(text):
    """Return (finding, detail) tuples if the notebook binds a download prefix to
    a bare predictable literal (B6-shape) that is NOT derived from a
    ``sample_data_bucket`` single-source variable."""
    src, _idx, _prev = _notebook_source_lines(text)
    if not src:
        return []
    hits = []
    for lineno, line in enumerate(src.splitlines(), start=1):
        if _has_nosem(line):
            continue
        # A prefix assignment (old_prefix = '...') bound to a bare predictable
        # s3:// literal.
        m = re.search(r"=\s*['\"]s3://(" + _BUCKET_ALTERNATION + r")", line)
        if not m:
            continue
        # Derived from the documented single-source variable? -> cleared.
        if "sample_data_bucket" in src:
            continue
        hits.append((lineno, line.strip()))
    return hits


# ---------------------------------------------------------------------------
# Layer 2 -- precise post-fix gate.
# ---------------------------------------------------------------------------
def _deploy_disallowed():
    """unverified_s3_access hits for deploy.py's SSM download list."""
    hits = []
    text = _read(DEPLOY_REL)
    if not text:
        return hits
    abs_path = os.path.join(REPO_ROOT, DEPLOY_REL)
    entries = extract_ssm_list(text)
    for _idx, bucket, entry in _block_unverified_accesses(entries):
        hits.append(Hit(
            "unverified_s3_access", abs_path, 0,
            f"SSM list entry accesses '{bucket}' with no preceding head-bucket "
            f"--expected-bucket-owner preflight: {entry.strip()}"))
    return hits


def _publish_disallowed():
    """unverified_s3_access hits for publish.sh's upload / docs-sync lines."""
    hits = []
    text = _read(PUBLISH_REL)
    if not text:
        return hits
    abs_path = os.path.join(REPO_ROOT, PUBLISH_REL)
    for lineno, bucket, line in _publish_unverified_accesses(text):
        hits.append(Hit(
            "unverified_s3_access", abs_path, lineno,
            f"publish.sh uploads to '{bucket}' with no preceding head-bucket "
            f"--expected-bucket-owner preflight: {line.strip()}"))
    return hits


def _index_rst_disallowed():
    """undocumented_doc_command hits for index.rst."""
    hits = []
    text = _read(INDEX_RST_REL)
    if not text:
        return hits
    abs_path = os.path.join(REPO_ROOT, INDEX_RST_REL)
    for lineno, line in _rst_undocumented_doc_commands(text):
        hits.append(Hit(
            "undocumented_doc_command", abs_path, lineno,
            f"documented aws s3 cp against a predictable bucket with no "
            f"ownership .. note:: / head-bucket preflight: {line}"))
    return hits


def _s3_rst_disallowed():
    """unverified_config_reference hits for s3.rst."""
    hits = []
    text = _read(S3_RST_REL)
    if not text:
        return hits
    abs_path = os.path.join(REPO_ROOT, S3_RST_REL)
    for lineno, line in _rst_unverified_config_refs(text):
        hits.append(Hit(
            "unverified_config_reference", abs_path, lineno,
            f"message-broker config bucket is a bare predictable literal (no "
            f"<placeholder>): {line}"))
    return hits


def _notebook_disallowed():
    """unverified_config_reference hits for the notebook."""
    hits = []
    text = _read(NOTEBOOK_REL)
    if not text:
        return hits
    abs_path = os.path.join(REPO_ROOT, NOTEBOOK_REL)
    for lineno, line in _notebook_unverified_refs(text):
        hits.append(Hit(
            "unverified_config_reference", abs_path, lineno,
            f"notebook download prefix bound to a bare predictable literal (no "
            f"sample_data_bucket variable): {line}"))
    return hits


def disallowed_hits():
    """The PRECISE post-fix pattern gate (task 7).

    A hit is *disallowed* only when it is in in-scope source, carries no
    documented ``# nosec`` / ``// nosec`` exception, and matches the precise
    design semantics for one of the three rules (unverified_s3_access,
    unverified_config_reference, undocumented_doc_command). The per-bucket
    preflight association parses ``deploy.py``'s SSM list and ``publish.sh``
    structurally, so a preflight only clears the SAME bucket that follows it.

    On the UNFIXED tree this is non-empty (the B1-B6 counterexamples); after the
    fix it must be empty (minus documented exceptions)."""
    hits = []
    hits.extend(_deploy_disallowed())
    hits.extend(_publish_disallowed())
    hits.extend(_index_rst_disallowed())
    hits.extend(_s3_rst_disallowed())
    hits.extend(_notebook_disallowed())
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
    print(f"S3-squat audit: {len(all_hits)} raw bug-condition hits "
          f"(vendored edgemlsdk/edgemlsdk & cdk.out excluded)\n")
    for label, frag in IN_SCOPE_SITES.items():
        site_hits = hits_for(frag, all_hits)
        print(f"=== {label} ({frag}) : {len(site_hits)} raw hit(s) ===")
        for h in site_hits:
            print(f"  [{h.category}] {_rel(h.path)}:{h.lineno}: {h.text.strip()}")
        print()

    disallowed = disallowed_hits()
    grouped = disallowed_by_category(disallowed)
    print("-" * 70)
    print(f"PATTERN GATE: {len(disallowed)} disallowed hit(s) in in-scope "
          f"source (must be 0 after fix, minus documented exceptions).")
    for category in sorted(grouped):
        print(f"  --- {category}: {len(grouped[category])} hit(s) ---")
        for h in grouped[category]:
            print(f"    {_rel(h.path)}:{h.lineno}: {h.text.strip()}")
    raise SystemExit(1 if disallowed else 0)
