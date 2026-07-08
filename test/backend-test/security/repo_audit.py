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

This module is the deterministic half of the bug-condition exploration test. It
runs ``grep -rn`` for the bug-condition patterns from the design over the
in-scope application code, EXCLUDING the generated CDK artifacts under
``edge-cv-portal/infrastructure/cdk.out/asset.*`` (which regenerate from source
and are out of scope).

On the UNFIXED tree this must surface NON-EMPTY hits across the eight sites (the
counterexamples that confirm the bug). The SAME audit becomes the pattern gate
in tasks 11/12 (it must then return zero *disallowed* hits, i.e. every remaining
occurrence must carry a documented ``# nosem`` justification).

Bug-condition grep patterns (from design "Repo-audit grep patterns"):
  * Shell / subprocess interpolation:
      ``subprocess\\.(run|call|check_output|check_call|Popen)`` combined with
      f-string / ``%`` / ``.format(`` / ``+``-built command args, and any
      ``shell=True``.
  * SSM shell docs: ``AWS-RunShellScript`` with f-string-built ``commands``.
  * Deserializers: ``\\bpickle\\b``, ``\\bdill\\b``, ``pickle\\.loads?``,
      ``dill\\.loads?``, ``torch\\.load\\(`` (flag any without
      ``weights_only=True``).
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


def disallowed_hits():
    """Hits that are NOT covered by a documented ``# nosem`` exception, and,
    for ``torch.load``, that lack ``weights_only=True`` on the same line.

    This is the form used by the post-fix pattern gate (tasks 11/12). On the
    UNFIXED tree it is still non-empty; after the fix it must be empty."""
    result = []
    for hit in run_audit():
        if _has_nosem(hit):
            continue
        if hit.category == "torch_load" and "weights_only=True" in hit.text.replace(" ", ""):
            continue
        result.append(hit)
    return result


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
        print(f"=== {label} ({frag}) : {len(site_hits)} hit(s) ===")
        for h in site_hits:
            print(f"  [{h.category}] {h.path}:{h.lineno}: {h.text.strip()}")
        print()
