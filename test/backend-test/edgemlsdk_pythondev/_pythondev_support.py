# Copyright 2026 Amazon Web Services, Inc.
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
"""Shared helpers for the ``edgemlsdk-python-dev-ubuntu2204`` spec tests.

Bug condition C(X): an apt install step X in ``src/edgemlsdk/Dockerfile``
requests a retired transitional Python package (concretely: ``python-dev`` at
line 286, ``RUN apt-get install python-dev -y``, Docker build step 61/83) that
has no installation candidate on the file's effective Ubuntu base (22.04 on
the AMD64 build servers) — apt exits 100 ("Package 'python-dev' has no
installation candidate ... replaced by: python2-dev python2
python-dev-is-python3") and the image build and portal build job die.
Verified live on portal build job ``08a1e2bd-45f9-4521-ac4a-b41b52222e2e``
(AMD64, dedicated, source_ref ``feature/workflow-triggers``, 2026-08-09).

These helpers parse the affected Dockerfiles as TEXT only — no ``docker``,
``subprocess``, or shell-out anywhere in this package (the spec's
no-Docker-builds validation constraint). Import-light so the tests run under
``pytest ... --noconftest`` without pulling in the backend package, mirroring
the proven ``edgemlsdk_cmake`` pattern.

Token-boundary discipline is load-bearing: ``python-dev`` is a proper prefix
of ``python-dev-is-python3``, so every scan matches whole package-name tokens
(whitespace-split after logical-RUN reconstruction), never substrings —
otherwise the fixed line would false-positive as still buggy.
"""
import os
import re

# edgemlsdk_pythondev/ -> backend-test/ -> test/ -> repo root (3 up).
REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)

DOCKERFILE_REL = "src/edgemlsdk/Dockerfile"
DOCKERFILE_JP5_REL = "src/edgemlsdk/Dockerfile.jp5"
DOCKERFILE_JP6_REL = "src/edgemlsdk/Dockerfile.jp6"

# The retired transitional Python package names (pre-Python-3-transition apt
# names retired during the Python 2 sunset; no installation candidate on
# jammy). Matched by EXACT whole-token equality only.
RETIRED_TRANSITIONAL_PYTHON_PACKAGES = frozenset(
    {"python-dev", "python", "python-pip", "python-setuptools"}
)

# Design Change 1 / Decision 1: the exact fixed form of the Triton-section
# single-package install step (apt's own named replacement; Depends:
# python3-dev + python-is-python3, which provides /usr/bin/python).
FIXED_TRITON_STEP = "RUN apt-get install python-dev-is-python3 -y"

# The concrete C(X) site on the unfixed tree (bugfix.md scan; live evidence
# job 08a1e2bd, Docker step 61/83, apt exit 100).
BUG_SITE_LINE = 286
BUG_SITE_TOKEN = "python-dev"


def read_repo_file(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# Dockerfile parsing (TEXT only)
# --------------------------------------------------------------------------- #
def parse_run_instructions(dockerfile_text):
    """Return every ``RUN`` instruction of a Dockerfile as
    ``(start_lineno, logical_text)``: backslash continuation lines joined,
    comment lines skipped (Docker skips ``#`` lines inside continuations),
    in file order. ``start_lineno`` is the 1-based physical line where the
    logical instruction begins."""
    logical = []
    pending = ""
    pending_start = None
    for lineno, raw in enumerate(dockerfile_text.splitlines(), start=1):
        if raw.lstrip().startswith("#"):
            # Comment line: never part of a logical instruction, and it does
            # not terminate a pending continuation.
            continue
        if pending:
            line = pending + raw
            start = pending_start
        else:
            line = raw
            start = lineno
        if line.rstrip().endswith("\\"):
            pending = line.rstrip()[:-1] + "\n"
            pending_start = start
            continue
        logical.append((start, line))
        pending = ""
        pending_start = None
    if pending:
        logical.append((pending_start, pending))
    return [
        (n, l) for n, l in logical if l.lstrip().upper().startswith("RUN ")
    ]


def shell_segments(run_text):
    """Split the shell command of a ``RUN`` instruction into simple command
    segments on ``&&``, ``||``, ``;`` and ``|``."""
    cmd = run_text.lstrip()[len("RUN "):]
    return [s for s in re.split(r"&&|\|\||;|\|", cmd) if s.strip()]


def apt_install_packages(segment):
    """If ``segment`` is an ``apt-get install`` / ``apt install`` command,
    return its requested package tokens (flags like ``-y``,
    ``--no-install-recommends``, ``--fix-missing`` excluded). Otherwise
    return None (e.g. ``apt-get update/remove/download``, ``make install``,
    ``./aws/install``). Tokens are whole whitespace-delimited words — strict
    token boundaries by construction."""
    tokens = segment.split()
    for i, tok in enumerate(tokens):
        if tok in ("apt-get", "apt"):
            j = i + 1
            while j < len(tokens) and tokens[j].startswith("-"):
                j += 1
            if j < len(tokens) and tokens[j] == "install":
                return [t for t in tokens[j + 1:] if not t.startswith("-")]
            return None
    return None


def apt_install_steps(dockerfile_text):
    """Every apt install step as ``(start_lineno, run_text, packages)`` —
    one entry per logical RUN instruction that contains at least one
    apt/apt-get install command (package lists merged per instruction)."""
    steps = []
    for lineno, run_text in parse_run_instructions(dockerfile_text):
        packages = []
        for seg in shell_segments(run_text):
            pkgs = apt_install_packages(seg)
            if pkgs:
                packages.extend(pkgs)
        if packages:
            steps.append((lineno, run_text, packages))
    return steps


def retired_token_sites(dockerfile_text):
    """The bug-condition scan: every ``(start_lineno, token, run_text)``
    where an apt install step requests a retired transitional Python package
    as a whole token. EXACT token equality — ``python-dev`` never
    substring-matches ``python-dev-is-python3`` or ``python3-dev``."""
    sites = []
    for lineno, run_text, packages in apt_install_steps(dockerfile_text):
        for pkg in packages:
            if pkg in RETIRED_TRANSITIONAL_PYTHON_PACKAGES:
                sites.append((lineno, pkg, run_text))
    return sites


def format_sites(rel_path, sites):
    return "\n".join(
        f"  {rel_path}:{n}: token {tok!r} in step: {run.strip()}"
        for n, tok, run in sites
    )


def find_step_with_packages(dockerfile_text, required):
    """First apt install step whose package tokens include every name in
    ``required`` — returns ``(index, (lineno, run_text, packages))`` within
    the RUN-instruction list, or (None, None)."""
    runs = parse_run_instructions(dockerfile_text)
    for idx, (lineno, run_text) in enumerate(runs):
        packages = []
        for seg in shell_segments(run_text):
            pkgs = apt_install_packages(seg)
            if pkgs:
                packages.extend(pkgs)
        if all(name in packages for name in required):
            return idx, (lineno, run_text, packages)
    return None, None


def triton_python_install_step(dockerfile_text):
    """The Triton-section single-package install step of
    ``src/edgemlsdk/Dockerfile``: the RUN instruction immediately following
    the ``rapidjson-dev libre2-dev`` install step ("Install Triton Server and
    it's dependencies"). Returns ``(lineno, run_text)``."""
    runs = parse_run_instructions(dockerfile_text)
    idx, found = find_step_with_packages(
        dockerfile_text, ("rapidjson-dev", "libre2-dev")
    )
    assert found is not None, (
        f"{DOCKERFILE_REL}: the Triton-section 'rapidjson-dev libre2-dev' "
        f"anchor step was not found — re-hypothesize the root cause."
    )
    assert idx + 1 < len(runs), (
        f"{DOCKERFILE_REL}: no RUN instruction follows the "
        f"'rapidjson-dev libre2-dev' step — re-hypothesize the root cause."
    )
    return runs[idx + 1]


def rm_usr_bin_python_command(dockerfile_text):
    """The downstream ``rm /usr/bin/python`` command: returns
    ``(start_lineno, segment_tokens)`` for the first RUN segment whose
    command is ``rm`` with ``/usr/bin/python`` among its arguments, or None.
    (The exact path token — ``rm -f /tmp/cmake.sh`` and ``rm /usr/bin/gcc``
    do not match.)"""
    for lineno, run_text in parse_run_instructions(dockerfile_text):
        for seg in shell_segments(run_text):
            toks = seg.split()
            if toks and toks[0] == "rm" and "/usr/bin/python" in toks:
                return lineno, toks
    return None
