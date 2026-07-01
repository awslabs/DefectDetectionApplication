#!/usr/bin/env python3
"""Bug-condition audit for the Python 3.11 security upgrade (spec:
python-3-11-security-upgrade).

This is the Task 1 "bug-condition exploration test" for the bug-condition
methodology. The bug is:

    isBugCondition(X) := dependsOnPython(X, "3.9")

i.e. any build / runtime / test / provisioning / doc artifact that installs,
selects, or runs the end-of-life (unsupported) Python 3.9 interpreter.

The audit enumerates every ``dependsOnPython(_, "3.9")`` reference across the
scoped artifact set named in the design. On the UNFIXED tree this audit finds
NON-EMPTY hits -- each hit is a counterexample that confirms the bug exists.

Two framings of the same data:

* Task 1  (exploration): run on the UNFIXED tree -> EXPECT non-empty hits.
  ``test_no_python39_dependency`` is written to assert the *fix* property
  (zero disallowed 3.9 hits). It therefore FAILS on the unfixed tree, and that
  failure surfaces the counterexamples. THIS IS THE EXPECTED EXPLORATION
  RESULT.
* Task 10 (fix checking): re-run the SAME audit on the FIXED tree -> EXPECT
  zero hits, so ``test_no_python39_dependency`` then PASSES.

Preservation (must NOT be flagged as fixes): the ``g-ir-scanner`` system-python
shebang (``/usr/bin/python3.8`` on JP5, dynamically-detected ``3.10`` on JP6)
and the host model-conversion ``python3`` (system) usage are distro-python
dependencies, NOT Python-3.9 dependencies. The audit patterns are deliberately
scoped so they do not match these (no ``python3.9`` token), and
``test_preserved_distro_python_not_flagged`` asserts that explicitly.

Run standalone:
    python3 test/python_version_audit.py

Run under pytest:
    python3 -m pytest test/python_version_audit.py -v
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

# Repository root = parent of this file's directory (test/..).
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Scoped artifact set (exactly the artifacts named in the design / tasks.md).
# ---------------------------------------------------------------------------
SCOPED_ARTIFACTS = [
    "build-custom.sh",
    "src/edgemlsdk/build.sh",
    "src/docker-compose.yaml",
    "src/backend/Dockerfile",
    "src/backend/Dockerfile.jp5",
    "src/backend/Dockerfile.jp6",
    "src/edgemlsdk/Dockerfile",
    "src/edgemlsdk/Dockerfile.jp5",
    "src/edgemlsdk/Dockerfile.jp6",
    "src/backend/edge_ml1_p_camera_management/install_edgemlsdk.sh",
    "src/backend/requirements.txt",
    "test/backend-test/conftest.py",
    "setup-build-server.sh",
    "station_install/setup_station.sh",
    "station_install/patch_docker_host_prereqs.sh",
    "README.md",
    "README_main.md",
    "test/README.md",
    "TEST_COVERAGE_PLAN.md",
]

# ---------------------------------------------------------------------------
# Bug-condition patterns: dependsOnPython(_, "3.9").
# Each pattern targets a concrete way an artifact selects / installs / runs the
# Python 3.9 interpreter. Patterns are intentionally specific so they do NOT
# match unrelated version numbers (e.g. react "^7.43.9", "libpython3.8.so") or
# the preserved distro-python references (python3.8 / bare python3 / dynamic
# 3.10).
# ---------------------------------------------------------------------------
PATTERNS = {
    # python3.9 interpreter invocations / installs / symlinks / venv paths.
    "python3.9 interpreter": re.compile(r"python3\.9"),
    # libpython3.9 ABI library (Triton / native-binding link target).
    "libpython3.9": re.compile(r"libpython3\.9"),
    # Triton pybind11 version pin.
    "PYBIND11_PYTHON_VERSION=3.9": re.compile(r"PYBIND11_PYTHON_VERSION\s*=\s*3\.9"),
    # PYTHONHOME bound to a 3.9 interpreter.
    "PYTHONHOME=...3.9": re.compile(r"PYTHONHOME.*3\.9"),
    # 3.9 get-pip.py bootstrap URL.
    "get-pip.py 3.9 URL": re.compile(r"bootstrap\.pypa\.io/pip/3\.9"),
    # Python-3.9.x from-source tarball / build dir.
    "Python-3.9 source tarball": re.compile(r"Python-3\.9"),
    # Build-orchestration args that select 3.9: `-y 3.9` and `python=3.9`.
    "-y 3.9 build arg": re.compile(r"-y\s+3\.9"),
    "python=3.9 default": re.compile(r"python\s*=\s*3\.9"),
    # Doc/prose & badge references that instruct using Python 3.9, e.g. the
    # `python-3.9+` README badge and "Python 3.9" prose. Catches a `python`
    # token followed by a space or hyphen and `3.9` (does not match the
    # preserved `python3.8` / `python 3.10` distro references).
    "Python 3.9 doc/prose": re.compile(r"[Pp]ython[ -]3\.9"),
}


@dataclass(frozen=True)
class Hit:
    artifact: str
    lineno: int
    pattern: str
    text: str


def _read_lines(path: str):
    with open(path, "r", encoding="utf-8", errors="replace") as fh:
        return fh.readlines()


def audit(root: str = REPO_ROOT, artifacts=SCOPED_ARTIFACTS):
    """Scan the scoped artifacts for bug-condition (3.9) references.

    Returns (hits, missing) where ``hits`` is a list[Hit] and ``missing`` is a
    list of scoped artifacts that were not found on disk.
    """
    hits: list[Hit] = []
    missing: list[str] = []
    for rel in artifacts:
        abs_path = os.path.join(root, rel)
        if not os.path.isfile(abs_path):
            missing.append(rel)
            continue
        for lineno, line in enumerate(_read_lines(abs_path), start=1):
            for label, regex in PATTERNS.items():
                if regex.search(line):
                    hits.append(Hit(rel, lineno, label, line.rstrip("\n")))
    return hits, missing


def format_report(hits, missing) -> str:
    lines = []
    lines.append("=" * 78)
    lines.append("BUG-CONDITION AUDIT  dependsOnPython(_, '3.9')  [python-3-11-security-upgrade]")
    lines.append("=" * 78)
    by_artifact: dict[str, list[Hit]] = {}
    for h in hits:
        by_artifact.setdefault(h.artifact, []).append(h)
    for artifact in SCOPED_ARTIFACTS:
        if artifact in by_artifact:
            ahits = by_artifact[artifact]
            lines.append(f"\n[{len(ahits)} hit(s)] {artifact}")
            for h in ahits:
                lines.append(f"    L{h.lineno:<4} ({h.pattern}): {h.text.strip()}")
    if missing:
        lines.append("\n(not found on disk, skipped): " + ", ".join(missing))
    lines.append("\n" + "-" * 78)
    lines.append(f"TOTAL counterexamples: {len(hits)} across "
                 f"{len(by_artifact)} artifact(s)")
    lines.append("-" * 78)
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Preservation reference checks (distro-python, must NOT be flagged as 3.9).
# ---------------------------------------------------------------------------
# The g-ir-scanner system-python shebang (3.8 on the JP5 focal base) and the
# host model-conversion bare `python3` usage are distro-python dependencies.
# These tokens must (a) exist in the tree and (b) NOT be matched by the 3.9
# bug-condition patterns above.
PRESERVED_REFERENCES = [
    # (token that should exist, file it should exist in)
    ("python3.8", "src/backend/Dockerfile.jp5"),  # g-ir-scanner distro shebang
]


def _contains(rel: str, token: str) -> bool:
    abs_path = os.path.join(REPO_ROOT, rel)
    if not os.path.isfile(abs_path):
        return False
    with open(abs_path, "r", encoding="utf-8", errors="replace") as fh:
        return token in fh.read()


# ---------------------------------------------------------------------------
# pytest entry points.
# ---------------------------------------------------------------------------
def test_no_python39_dependency():
    """FIX PROPERTY: no scoped artifact depends on Python 3.9.

    On the UNFIXED tree this FAILS (non-empty counterexamples) -> confirms the
    bug. On the FIXED tree (task 10) this PASSES (zero hits).
    """
    hits, missing = audit()
    report = format_report(hits, missing)
    assert not hits, (
        "Bug confirmed: scoped artifacts still depend on Python 3.9.\n" + report
    )


def test_preserved_distro_python_not_flagged():
    """PRESERVATION: distro-python references exist and are NOT 3.9 hits.

    The g-ir-scanner system-python shebang (python3.8) must be present and must
    not be matched by any bug-condition (3.9) pattern.
    """
    for token, rel in PRESERVED_REFERENCES:
        assert _contains(rel, token), (
            f"Expected preserved distro-python reference '{token}' in {rel}"
        )
        for label, regex in PATTERNS.items():
            assert not regex.search(token), (
                f"Preserved reference '{token}' wrongly matched 3.9 pattern '{label}'"
            )


if __name__ == "__main__":
    found, not_found = audit()
    print(format_report(found, not_found))
    # Exit non-zero when the bug is present so the audit doubles as a CI gate
    # (task 14). On the unfixed tree this prints the counterexamples and exits 1.
    raise SystemExit(1 if found else 0)
