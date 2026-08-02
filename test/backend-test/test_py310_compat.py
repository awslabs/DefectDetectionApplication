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
"""Python 3.10/3.11 source-compatibility audit of ``src/backend``.

Feature: jp6-vllm-enablement
Requirements 2.6 / 2.7 (design.md section 7 "Python 3.10/3.11
source-compatibility audit"):

    "THE shared ``src/backend`` application code SHALL be source-compatible
    with CPython 3.10 and CPython 3.11, verified by an automated check of the
    ``src/backend`` tree for 3.11-only syntax and 3.11-only standard-library
    usage."

The JP6 image runs the DDA backend under CPython 3.10 (the Jetson AI Lab vLLM
wheels are cp310-only) while JP5/x86 images stay on CPython 3.11, so the shared
source must parse and run on BOTH interpreters. Two gates enforce this:

1. **Syntax gate** — ``ast.parse(source, feature_version=(3, 10))`` over every
   ``*.py`` under ``src/backend``. The compile-time grammar check rejects
   3.11-only syntax (e.g. ``except*`` / ``TryStar``) regardless of which
   interpreter executes this test.
2. **Stdlib gate** — an AST scan of the same tree for a denylist of 3.11-only
   standard-library names that would import/attribute-resolve fine on 3.11 but
   fail at runtime on 3.10: ``tomllib``, ``asyncio.TaskGroup`` /
   ``asyncio.timeout``, ``typing.Self`` / ``typing.LiteralString``,
   ``enum.StrEnum``, ``datetime.UTC``, ``contextlib.chdir``.

Scope (design.md): vendored third-party code under ``src/backend/edgemlsdk``
is EXCLUDED (not app source; not installed in the backend image), while
``src/backend/workflow_engine/vendor`` is INCLUDED (it ships in the image and
imports at runtime).
"""
import ast
import os

# backend-test/ -> test/ -> repo root (2 up).
REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
)
BACKEND_ROOT = os.path.join(REPO_ROOT, "src", "backend")

# Directory subtrees excluded from the audit, relative to src/backend.
# edgemlsdk is vendored third-party build tooling, not app source.
EXCLUDED_SUBTREES = ("edgemlsdk",)

# 3.11-only members of stdlib modules: importing or attribute-accessing these
# fails at runtime under CPython 3.10.
PY311_ONLY_MODULES = frozenset({"tomllib"})
PY311_ONLY_MEMBERS = {
    "asyncio": frozenset({"TaskGroup", "timeout"}),
    "typing": frozenset({"Self", "LiteralString"}),
    "enum": frozenset({"StrEnum"}),
    "datetime": frozenset({"UTC"}),
    "contextlib": frozenset({"chdir"}),
}


def _audited_files():
    """Every ``*.py`` under src/backend, minus the excluded subtrees."""
    files = []
    for dirpath, dirnames, filenames in os.walk(BACKEND_ROOT):
        rel = os.path.relpath(dirpath, BACKEND_ROOT)
        parts = [] if rel == "." else rel.split(os.sep)
        if parts and parts[0] in EXCLUDED_SUBTREES:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in sorted(filenames):
            if name.endswith(".py"):
                files.append(os.path.join(dirpath, name))
    return sorted(files)


def _read_source(path):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.read()


def _rel(path):
    return os.path.relpath(path, REPO_ROOT)


def _py311_stdlib_violations(tree):
    """Return human-readable violations of the 3.11-only stdlib denylist."""
    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in PY311_ONLY_MODULES:
                    violations.append(
                        "line %d: import %s (3.11-only module)"
                        % (node.lineno, alias.name)
                    )
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            top = module.split(".")[0]
            # Relative imports (level > 0) are package-local, never stdlib.
            if node.level:
                continue
            if top in PY311_ONLY_MODULES:
                violations.append(
                    "line %d: from %s import ... (3.11-only module)"
                    % (node.lineno, module)
                )
            elif top in PY311_ONLY_MEMBERS:
                banned = PY311_ONLY_MEMBERS[top]
                for alias in node.names:
                    if alias.name in banned:
                        violations.append(
                            "line %d: from %s import %s (3.11-only)"
                            % (node.lineno, module, alias.name)
                        )
        elif isinstance(node, ast.Attribute):
            # Catch dotted usage such as asyncio.TaskGroup(...) or
            # datetime.UTC that never appears in an import statement.
            if isinstance(node.value, ast.Name):
                banned = PY311_ONLY_MEMBERS.get(node.value.id)
                if banned and node.attr in banned:
                    violations.append(
                        "line %d: %s.%s (3.11-only)"
                        % (node.lineno, node.value.id, node.attr)
                    )
    return violations


def test_audit_scope_is_sane():
    """The walk must find the backend tree, include workflow_engine/vendor,
    and exclude edgemlsdk — guards against silent pass on path drift."""
    files = _audited_files()
    assert files, "audit found no *.py files under src/backend — path drift?"
    rels = {os.path.relpath(f, BACKEND_ROOT).replace(os.sep, "/") for f in files}
    assert any(r.startswith("workflow_engine/vendor/") for r in rels), (
        "workflow_engine/vendor must be included in the audit scope"
    )
    assert not any(r.startswith("edgemlsdk/") for r in rels), (
        "edgemlsdk must be excluded from the audit scope"
    )


def test_backend_sources_parse_as_python_310():
    """Syntax gate: every src/backend source parses under the 3.10 grammar.

    Validates: Requirements 2.6, 2.7
    """
    failures = []
    for path in _audited_files():
        source = _read_source(path)
        try:
            ast.parse(source, filename=path, feature_version=(3, 10))
        except SyntaxError as exc:
            failures.append("%s: line %s: %s" % (_rel(path), exc.lineno, exc.msg))
    assert not failures, (
        "%d file(s) use syntax not valid on CPython 3.10 (JP6 DDA "
        "interpreter):\n  %s" % (len(failures), "\n  ".join(failures))
    )


def test_backend_sources_avoid_py311_only_stdlib():
    """Stdlib gate: no 3.11-only standard-library imports or attribute usage.

    Validates: Requirements 2.6, 2.7
    """
    failures = []
    for path in _audited_files():
        source = _read_source(path)
        try:
            tree = ast.parse(source, filename=path)
        except SyntaxError:
            # The syntax gate reports parse problems; skip here.
            continue
        for violation in _py311_stdlib_violations(tree):
            failures.append("%s: %s" % (_rel(path), violation))
    assert not failures, (
        "%d 3.11-only stdlib usage(s) found in src/backend (would fail at "
        "runtime on the JP6 3.10 DDA interpreter):\n  %s"
        % (len(failures), "\n  ".join(failures))
    )
