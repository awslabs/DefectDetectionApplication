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
"""Repo audit for the injection / unsafe-deserialization bug-condition patterns
(security-injection-deserialization-fixes, finding #9 / Req 2.9).

This module has two layers that intentionally differ in breadth:

* ``run_audit()`` — the RAW, broad enumeration. It ``grep``s the whole in-scope
  tree (only ``cdk.out/asset.*`` excluded) for every bug-condition token. On the
  UNFIXED tree it surfaces NON-EMPTY hits across the eight sites (the
  counterexamples that confirm the bug); task 1 uses it to list them. It applies
  NO precision filtering, so it also matches comments, docstrings, safe arg-list
  calls, etc. — that is by design for the exploration phase.

* ``disallowed_hits()`` — the PRECISE pattern gate re-run in tasks 11/12. It
  implements the exact design semantics below so that ONLY a genuinely unsafe,
  un-neutralized occurrence in in-scope application code counts. A bare arg-list
  ``subprocess.run([...])`` with no interpolation and no ``shell=True``, a
  literal ``DocumentName="AWS-RunShellScript"`` with no f-string-built command, a
  ``torch.load(..., weights_only=True)`` call, a comment/docstring mention, and
  any occurrence in an out-of-scope file are NOT disallowed. After the fix this
  must return zero (minus documented ``# nosem`` exceptions).

Precise gate semantics (from design "Repo-audit grep patterns", ~L562-575):
  * Shell / subprocess interpolation: a
    ``subprocess\\.(run|call|check_output|check_call|Popen)`` call is disallowed
    ONLY when the command argument is built by interpolation — an f-string
    (``f"``/``f'``), ``%`` formatting, ``.format(``, or ``+`` string
    concatenation — OR ``shell=True`` is present. Bare arg-list calls are safe.
  * SSM shell docs: ``AWS-RunShellScript`` is disallowed ONLY when the same line
    carries interpolation building the ``commands`` (a bare literal document
    name whose args are ``shlex.quote``'d elsewhere is safe).
  * Deserializers: ``\\bpickle\\b``, ``\\bdill\\b``, ``pickle\\.loads?``,
    ``dill\\.loads?`` in real code are disallowed; ``torch\\.load\\(`` is
    disallowed ONLY when ``weights_only=True`` is absent on the call.
  * Comment / docstring lines never count (they carry no live sink).

In-scope-source scoping: the gate is asserted over the application code this spec
owns — the eight finding sites plus the two ``#3`` ``run_command`` caller modules
(the design "Finding traceability" list) — NOT over the security test/fixture
files themselves (which legitimately contain the pattern strings and crafted
payloads), NOT over the duplicate vendored ``edgemlsdk/edgemlsdk/`` tree, NOT
over the generated ``cdk.out/asset.*`` artifacts, and NOT over files owned by
other specs (e.g. the Triton ``inference_runtimes.py`` ``torch.load``). Precision
+ this scoping — not a hard-coded list of the current lines — is what lets the
gate still FAIL if an interpolated subprocess/SSM command or an unsafe
deserializer is reintroduced into any of the real fixed source files.
"""
import os
import re
import subprocess
from collections import namedtuple

# repo_audit.py lives at test/backend-test/security/ -> repo root is 3 up.
REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)

# Generated CDK build artifacts: out of scope, must be excluded from the audit.
EXCLUDED_PATH_SUBSTRING = os.path.join("cdk.out", "asset.")

Hit = namedtuple("Hit", ["category", "path", "lineno", "text"])

# Each entry: (category, extended-regexp). These mirror the design's
# "Repo-audit grep patterns" section.
AUDIT_PATTERNS = [
    ("subprocess_call", r"subprocess\.(run|call|check_output|check_call|Popen)\("),
    ("shell_true", r"shell\s*=\s*True"),
    ("ssm_runshellscript", r"AWS-RunShellScript"),
    ("pickle_module", r"\bpickle\b"),
    ("dill_module", r"\bdill\b"),
    ("pickle_loads", r"pickle\.loads?\("),
    ("dill_load", r"dill\.loads?\("),
    ("torch_load", r"torch\.load\("),
]

# Directories that never contain in-scope application code. Excluding these
# keeps the audit fast and free of vendored / generated noise.
EXCLUDE_DIRS = [".git", "node_modules", ".hypothesis", "__pycache__", ".venv", "venv"]


def _grep(pattern):
    """Run ``grep -rnE`` for a single pattern over the repo's Python files and
    return raw ``path:lineno:text`` lines (excluded paths already filtered)."""
    cmd = ["grep", "-rnE", "--include=*.py"]
    for d in EXCLUDE_DIRS:
        cmd.append("--exclude-dir=" + d)
    cmd.extend([pattern, REPO_ROOT])
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # grep exit code 1 == "no matches", which is not an error for us.
    if proc.returncode not in (0, 1):
        raise RuntimeError(f"grep failed ({proc.returncode}): {proc.stderr}")
    return proc.stdout.splitlines()


def _parse_line(category, raw):
    # Format: <path>:<lineno>:<text>
    m = re.match(r"^(.*?):(\d+):(.*)$", raw)
    if not m:
        return None
    path, lineno, text = m.group(1), int(m.group(2)), m.group(3)
    return Hit(category=category, path=path, lineno=lineno, text=text)


def run_audit():
    """Return all raw audit Hits across the in-scope tree (cdk.out/asset.*
    excluded). No ``# nosem`` filtering is applied here -- this is the raw
    bug-condition enumeration used by the exploration test."""
    hits = []
    for category, pattern in AUDIT_PATTERNS:
        for raw in _grep(pattern):
            if EXCLUDED_PATH_SUBSTRING in raw:
                continue
            hit = _parse_line(category, raw)
            if hit is None:
                continue
            # Belt-and-suspenders: skip anything under the excluded artifacts.
            if EXCLUDED_PATH_SUBSTRING in hit.path:
                continue
            hits.append(hit)
    return hits


def _has_nosem(hit):
    """True if the hit line carries a documented suppression marker."""
    return "nosem" in hit.text.lower() or "noqa" in hit.text.lower()


# In-scope application code the pattern gate is asserted over: the eight finding
# sites plus the two #3 ``run_command`` caller modules (design "Finding
# traceability"). Paths are relative to REPO_ROOT. Scoping the gate to these
# files (and applying the precise per-line semantics below inside them) keeps
# out the security test/fixture files, the duplicate vendored
# ``edgemlsdk/edgemlsdk/`` tree, and files owned by other specs, while still
# catching a reintroduced disallowed pattern in any real fixed source file.
IN_SCOPE_FILES = frozenset(
    os.path.normpath(p)
    for p in (
        os.path.join("src", "backend", "snapshot", "Snapshotter.py"),
        os.path.join("src", "edgemlsdk", "src", "test", "longevity", "deploy.py"),
        os.path.join("src", "backend", "utils", "utils.py"),
        os.path.join("src", "backend", "utils", "user_group_management_utils.py"),
        os.path.join("src", "backend", "utils", "filesystem_management_utils.py"),
        os.path.join(
            "test", "backend-test", "host_scripts", "test_docker_profile_selection.py"
        ),
        os.path.join(
            "src", "backend", "lyra_science_processing_utils", "model_processors",
            "supervised_bbox_stage1_postprocessor.py",
        ),
        os.path.join("src", "backend", "utils", "camera_manager.py"),
        os.path.join("src", "backend", "utils", "digital_input_process_manager.py"),
        os.path.join("edge-cv-portal", "backend", "functions", "model_converter.py"),
    )
)


def _is_in_scope(hit):
    """True if the hit is in application code this spec owns (see IN_SCOPE_FILES).

    Uses the exact repo-relative path so the duplicate vendored
    ``src/backend/edgemlsdk/edgemlsdk/...`` copies do NOT match the real
    ``src/edgemlsdk/...`` sources."""
    rel = os.path.normpath(os.path.relpath(hit.path, REPO_ROOT))
    return rel in IN_SCOPE_FILES


def _is_comment_line(text):
    """True if the source line is a pure comment (no live sink)."""
    return text.lstrip().startswith("#")


def _has_interpolation(text):
    """True if the line builds a string via f-string / ``%`` / ``.format(`` /
    ``+`` concatenation -- i.e. a command argument built by interpolation."""
    if re.search(r"\b[rRbB]?f['\"]", text):  # f-string / rf-string
        return True
    if ".format(" in text:
        return True
    # %-formatting: "%s"/"%d"/... or a "..." % (...) style expression.
    if re.search(r"%[sdrfxeg%]", text) or re.search(r"['\"]\s*%\s*[\(\w]", text):
        return True
    # String concatenation building an argument: "..." +  /  + "..." .
    if re.search(r"['\"]\s*\+", text) or re.search(r"\+\s*['\"]", text):
        return True
    return False


def _has_shell_true(text):
    return re.search(r"shell\s*=\s*True", text) is not None


def _is_disallowed(hit):
    """Apply the PRECISE design semantics to a single raw hit."""
    # Documented, justified exceptions (the #4 test line, any #6 fallback).
    if _has_nosem(hit):
        return False
    # Comments / docstring-comment lines carry no live sink.
    if _is_comment_line(hit.text):
        return False

    if hit.category in ("subprocess_call",):
        # Disallowed only when the command arg is interpolated or shell=True.
        return _has_interpolation(hit.text) or _has_shell_true(hit.text)

    if hit.category == "shell_true":
        # Any real ``shell=True`` in code (comments already filtered out).
        return _has_shell_true(hit.text)

    if hit.category == "ssm_runshellscript":
        # Disallowed only when the ``commands`` are f-string / interpolation
        # built on the line; a bare ``DocumentName="AWS-RunShellScript"`` is safe.
        return _has_interpolation(hit.text)

    if hit.category == "torch_load":
        # Disallowed only when the effective call lacks ``weights_only=True``.
        return "weights_only=True" not in hit.text.replace(" ", "")

    # Remaining deserializer tokens (pickle / dill) in real code are disallowed.
    if hit.category in ("pickle_module", "dill_module", "pickle_loads", "dill_load"):
        return True

    return True


def disallowed_hits():
    """The PRECISE post-fix pattern gate (tasks 11/12).

    A raw ``run_audit()`` hit is *disallowed* only when ALL of the following hold:
      * it is in in-scope application code (``IN_SCOPE_FILES``) -- the security
        test/fixtures, the vendored ``edgemlsdk/edgemlsdk/`` duplicate, the
        generated ``cdk.out/asset.*`` artifacts, and other specs' files are out
        of scope;
      * it is not a comment/docstring line and carries no documented ``# nosem``;
      * it matches the precise design semantics for its category (subprocess:
        interpolation or ``shell=True``; SSM: f-string-built commands;
        ``torch.load``: missing ``weights_only=True``; ``pickle``/``dill`` in
        real code).

    On the UNFIXED tree this is non-empty (the eight counterexamples); after the
    fix it must be empty (minus documented exceptions)."""
    return [
        hit for hit in run_audit()
        if _is_in_scope(hit) and _is_disallowed(hit)
    ]


def hits_for(path_substring, hits=None):
    """All hits whose file path contains ``path_substring``."""
    hits = run_audit() if hits is None else hits
    return [h for h in hits if path_substring in h.path]


# The eight in-scope findings -> a path fragment that uniquely identifies the
# real source file (NOT the generated cdk.out/asset.* copies).
IN_SCOPE_SITES = {
    "#1 Snapshotter": os.path.join("src", "backend", "snapshot", "Snapshotter.py"),
    "#2 deploy.py": os.path.join("edgemlsdk", "src", "test", "longevity", "deploy.py"),
    "#3 utils.run_command": os.path.join("src", "backend", "utils", "utils.py"),
    "#4 docker_profile test": os.path.join(
        "test", "backend-test", "host_scripts", "test_docker_profile_selection.py"
    ),
    "#5 bbox postprocessor": os.path.join(
        "model_processors", "supervised_bbox_stage1_postprocessor.py"
    ),
    "#6 camera_manager": os.path.join("src", "backend", "utils", "camera_manager.py"),
    "#7 dio process manager": os.path.join(
        "src", "backend", "utils", "digital_input_process_manager.py"
    ),
    "#8 model_converter": os.path.join(
        "edge-cv-portal", "backend", "functions", "model_converter.py"
    ),
}


if __name__ == "__main__":
    all_hits = run_audit()
    print(f"Repo audit: {len(all_hits)} raw bug-condition hits "
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
          f"application code (must be 0, minus documented # nosem).")
    for h in disallowed:
        rel = os.path.relpath(h.path, REPO_ROOT)
        print(f"  [{h.category}] {rel}:{h.lineno}: {h.text.strip()}")
    raise SystemExit(1 if disallowed else 0)
