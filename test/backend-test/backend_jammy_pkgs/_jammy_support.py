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
"""Shared helpers for the ``backend-jammy-retired-packages`` spec tests.

Bug condition C(X): an apt install step X in the docker-compose AMD64 build
path (unconditional, or reachable when the effective base is Ubuntu 22.04)
requests a package with no installation candidate on the jammy base, so apt
exits 100 and the image build fails. Concretely today:
``src/backend/Dockerfile`` line 70's ``RUN apt-get install libssl1.1 -y``
(target ``backend_generic``, Docker build step 24/63) — apt reports
"Unable to locate package libssl1.1" and the image build and portal build
job die. Verified live on portal build job
``3d18ba88-9c17-490a-811b-8c21360216f4`` (AMD64, dedicated X86 server,
source_ref ``feature/workflow-triggers``, commit ``4e1ce8c``, settled
``failed`` / ``BUILD_FAILED`` on 2026-08-09 at ~21m51s).

These helpers parse the Dockerfiles and install scripts as TEXT only — no
``docker``, ``subprocess``, or shell-out anywhere in this package (the
spec's no-Docker-builds validation constraint). Import-light so the tests
run under ``pytest ... --noconftest``, mirroring the proven
``edgemlsdk_pythondev`` pattern.

Token-boundary discipline is load-bearing: ``libssl1.1`` must only match as
an exact whole package-name token — ``libssl-dev`` (Dockerfile line 4) and
``libssl3`` (jammy's OpenSSL runtime) must NEVER classify as retired, and a
hypothetical ``libssl1.1-dbg`` must not substring-match. All scans match
whole whitespace-delimited tokens after logical-line reconstruction.

Reachability model (release-conditional modeling, per design):

- an UNCONDITIONAL apt install step is reachable on every base;
- a step inside an ``. /etc/os-release``-sourced conditional comparing
  ``"$VERSION_ID"`` against quoted release strings is reachable only on the
  releases in that allowlist (the fixed step's shape: allowlist
  {"18.04", "20.04"}, hence 22.04-UNreachable);
- the existing ``if [ "$OS" = "18.04" ]`` guard (Dockerfile lines 73-75) is
  modeled as 18.04-only — its nominal allowlist. (In reality it is inert on
  every base because ``ARG OS`` is declared only before ``FROM`` and ``$OS``
  is out of scope in RUN; the model's 18.04-only reading is the conservative
  superset and keeps the step 22.04-unreachable either way.)
"""
import os
import re
from collections import namedtuple

# backend_jammy_pkgs/ -> backend-test/ -> test/ -> repo root (3 up).
REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)

BACKEND_DOCKERFILE_REL = "src/backend/Dockerfile"
FRONTEND_DOCKERFILE_REL = "src/frontend/Dockerfile"
# The three install scripts the backend Dockerfile invokes (located via its
# RUN lines: ./edge_ml1_p_camera_management/<script>).
PREREQS_SCRIPT_REL = "src/backend/edge_ml1_p_camera_management/prereqs_install.sh"
ARAVIS_SCRIPT_REL = "src/backend/edge_ml1_p_camera_management/install_aravis.sh"
EDGEMLSDK_SCRIPT_REL = (
    "src/backend/edge_ml1_p_camera_management/install_edgemlsdk.sh"
)
INSTALL_SCRIPTS_REL = (
    PREREQS_SCRIPT_REL,
    ARAVIS_SCRIPT_REL,
    EDGEMLSDK_SCRIPT_REL,
)

# Jammy-retired package names: no installation candidate on Ubuntu 22.04.
# Matched by EXACT whole-token equality only (never substrings): libssl-dev
# and libssl3 must never classify as retired.
RETIRED_JAMMY_PACKAGES = frozenset({"libssl1.1"})

# The concrete C(X) site on the unfixed tree (bugfix.md scan; live evidence
# job 3d18ba88, backend Docker step 24/63, apt exit 100). Content-matched,
# never line-number-matched (the fix shifts later line numbers).
BUG_SITE_TOKEN = "libssl1.1"
UNFIXED_BUG_STEP = "RUN apt-get install libssl1.1 -y"

# Design Change 1: the fixed step's contract — an /etc/os-release-gated
# release conditional with EXACTLY this allowlist and this apt body.
FIXED_LIBSSL_ALLOWLIST = frozenset({"18.04", "20.04"})
FIXED_LIBSSL_BODY = "apt-get install libssl1.1 -y"

# Class-closure verdict inventory (design Decision 2 + the bugfix.md scan
# table): every package token requested by an AMD64-reachable apt step in
# the compose build path, verified to have an installation candidate on the
# jammy base (live-green in build 3d18ba88 before the failure point, or
# verified against the real jammy index in a container on the exact base
# image public.ecr.aws/ubuntu/ubuntu:22.04 during the design phase).
# ``libssl1.1`` is deliberately NOT here — it is the retired set's member.
# Any token outside this inventory (and outside the retired set) fails the
# class-closure test until vetted.
JAMMY_RESOLVABLE_INVENTORY = frozenset({
    # src/backend/Dockerfile line 4 (ran live green in 3d18ba88)
    "software-properties-common", "wget", "build-essential", "cmake",
    "libffi-dev", "zlib1g-dev", "libssl-dev", "libsqlite3-dev", "gdb",
    # lines 16-18: jammy's native toolchain (ran live green)
    "gcc-11", "g++-11",
    # lines 24-25: --only-upgrade steps (ran live green)
    "ncurses-base", "ncurses-bin",
    # line 26: jammy universe (ran live green)
    "libb64-0d",
    # prereqs_install.sh (ran live green; libgl1-mesa-glx transitional on
    # jammy but a valid candidate — retired later, in 23.04+)
    "pkgconf", "libcairo2-dev", "libgirepository1.0-dev", "libgl1-mesa-glx",
    "libsm6", "libxext6",
    # install_aravis.sh (ran live green)
    "ninja-build", "gstreamer1.0-plugins-bad", "libxml2-dev",
    "libglib2.0-dev", "libusb-1.0-0-dev", "gobject-introspection",
    "libgtk-3-dev", "gtk-doc-tools", "xsltproc", "libgstreamer1.0-dev",
    "libgstreamer-plugins-base1.0-dev", "gstreamer1.0-plugins-good",
    "gettext", "pkg-config",
    # line 72 (design Decision 2: verified against the jammy index —
    # libexif12 0.6.24-1ubuntu0.22.04.1, libcurl4 7.81.0, libarchive13
    # 3.6.0, gstreamer1.0-tools 1.20.3, gstreamer1.0-libav 1.20.3,
    # ffmpeg 7:4.4.2)
    "libexif12", "libcurl4", "libarchive13", "gstreamer1.0-tools",
    "gstreamer1.0-libav", "ffmpeg",
    # CVE block (design Decision 2: build-essential 12.9ubuntu3,
    # libgnutls28-dev 3.7.3, libuv1 1.43.0)
    "libgnutls28-dev", "libuv1",
})

# An apt install step in the AMD64 compose build path.
#   path:      repo-relative file
#   lineno:    1-based physical line where the logical line begins
#   text:      the full logical line/instruction text
#   packages:  requested package tokens (flags excluded)
#   allowlist: None for unconditional (reachable everywhere), else a
#              frozenset of release strings the guard allows
AptStep = namedtuple("AptStep", "path lineno text packages allowlist")


def read_repo_file(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


# --------------------------------------------------------------------------- #
# Logical-line reconstruction (TEXT only)
# --------------------------------------------------------------------------- #
def logical_lines(text):
    """Join backslash-continuation lines, skip whole-line comments (Docker
    skips ``#`` lines inside continuations too), return
    ``(start_lineno, logical_text)`` in file order. Shared by the Dockerfile
    RUN reconstruction and the shell-script scan."""
    out = []
    pending = ""
    pending_start = None
    for lineno, raw in enumerate(text.splitlines(), start=1):
        if raw.lstrip().startswith("#"):
            # Comment line: never part of a logical line, and it does not
            # terminate a pending continuation.
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
        out.append((start, line))
        pending = ""
        pending_start = None
    if pending:
        out.append((pending_start, pending))
    return out


def parse_run_instructions(dockerfile_text):
    """Every ``RUN`` instruction of a Dockerfile as
    ``(start_lineno, logical_text)`` — logical RUN reconstruction across
    backslash continuations, comments skipped, in file order."""
    return [
        (n, l)
        for n, l in logical_lines(dockerfile_text)
        if l.lstrip().upper().startswith("RUN ")
    ]


def shell_segments(command_text):
    """Split a shell command into simple command segments on ``&&``, ``||``,
    ``;`` and ``|``."""
    return [s for s in re.split(r"&&|\|\||;|\|", command_text) if s.strip()]


def apt_install_packages(segment):
    """If ``segment`` is an ``apt-get install`` / ``apt install`` command,
    return its requested package tokens (flags like ``-y``,
    ``--no-install-recommends``, ``--only-upgrade`` excluded). Otherwise
    return None (e.g. ``apt-get update``, ``apt clean``, ``dpkg -i``,
    ``pip install``). Tokens are whole whitespace-delimited words — strict
    token boundaries by construction: ``libssl1.1`` can only match exactly,
    and ``libssl-dev``/``libssl3`` are distinct tokens."""
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


def normalized_apt_bodies(command_text):
    """The normalized apt command bodies of a shell command: whitespace
    collapsed and leading shell keywords (``then``/``else``/``do``) stripped,
    so the fixed guarded form's apt segment (``then apt-get install
    libssl1.1 -y`` after ``;``-splitting) compares equal to
    ``FIXED_LIBSSL_BODY``. Mirrors
    ``_jammy_preservation_support._apt_bodies``."""
    bodies = []
    for seg in shell_segments(command_text):
        if apt_install_packages(seg):
            toks = seg.split()
            while toks and toks[0] in ("then", "else", "do"):
                toks = toks[1:]
            bodies.append(" ".join(toks))
    return bodies


# --------------------------------------------------------------------------- #
# Release-conditional reachability modeling
# --------------------------------------------------------------------------- #
_VERSION_ID_CMP = re.compile(r'"\$VERSION_ID"\s*=\s*"([0-9]+\.[0-9]+)"')
_OS_ARG_CMP = re.compile(r'"\$OS"\s*=\s*"([0-9]+\.[0-9]+)"')
_OS_RELEASE_SOURCED = re.compile(r"\.\s+/etc/os-release")


def guard_allowlist(logical_text):
    """The release allowlist guarding the apt commands of a logical line:

    - ``. /etc/os-release`` + ``if`` comparisons of ``"$VERSION_ID"``
      against quoted releases -> frozenset of those releases (the fixed
      step's shape);
    - ``if`` comparisons of ``"$OS"`` against quoted releases -> frozenset
      of those releases (the lines-73-75 shape, modeled 18.04-only);
    - no release guard -> None (unconditional: reachable everywhere).
    """
    if "if" in logical_text.split():
        versions = _VERSION_ID_CMP.findall(logical_text)
        if versions and _OS_RELEASE_SOURCED.search(logical_text):
            return frozenset(versions)
        os_versions = _OS_ARG_CMP.findall(logical_text)
        if os_versions:
            return frozenset(os_versions)
    return None


def is_reachable(step, base):
    """A guarded step is reachable iff ``base`` is in its allowlist; an
    unconditional step is reachable on every base."""
    return step.allowlist is None or base in step.allowlist


def _steps_from_logical(path_rel, logicals, strip_run):
    steps = []
    for lineno, text in logicals:
        cmd = text.lstrip()[len("RUN "):] if strip_run else text
        packages = []
        for seg in shell_segments(cmd):
            pkgs = apt_install_packages(seg)
            if pkgs:
                packages.extend(pkgs)
        if packages:
            steps.append(
                AptStep(path_rel, lineno, text, packages, guard_allowlist(text))
            )
    return steps


def dockerfile_apt_steps(rel_path):
    """Every apt install step of a Dockerfile as ``AptStep`` records — one
    per logical RUN instruction containing at least one apt/apt-get install
    command (package lists merged per instruction)."""
    text = read_repo_file(rel_path)
    return _steps_from_logical(rel_path, parse_run_instructions(text), True)


def script_apt_steps(rel_path):
    """Every apt install step of a shell script as ``AptStep`` records —
    logical lines reconstructed across backslash continuations."""
    text = read_repo_file(rel_path)
    return _steps_from_logical(rel_path, logical_lines(text), False)


def amd64_build_path_apt_steps():
    """All apt install steps of the docker-compose AMD64 build path:
    ``src/backend/Dockerfile`` plus the three install scripts it invokes."""
    steps = dockerfile_apt_steps(BACKEND_DOCKERFILE_REL)
    for rel in INSTALL_SCRIPTS_REL:
        steps.extend(script_apt_steps(rel))
    return steps


# --------------------------------------------------------------------------- #
# Scans and site formatting
# --------------------------------------------------------------------------- #
def retired_reachable_sites(base="22.04"):
    """The bug-condition scan: every ``(step, token)`` where an apt install
    step reachable on ``base`` requests a jammy-retired package as a whole
    token. EXACT token equality — ``libssl1.1`` never substring-matches, and
    ``libssl-dev``/``libssl3`` never classify as retired."""
    sites = []
    for step in amd64_build_path_apt_steps():
        if not is_reachable(step, base):
            continue
        for pkg in step.packages:
            if pkg in RETIRED_JAMMY_PACKAGES:
                sites.append((step, pkg))
    return sites


def format_sites(sites):
    return "\n".join(
        f"  {step.path}:{step.lineno}: token {tok!r} in step: "
        f"{step.text.strip()}"
        for step, tok in sites
    )


def libssl_install_step():
    """The libssl install step of ``src/backend/Dockerfile``: the apt step
    whose requested packages include the exact token ``libssl1.1``
    (content-matched, not line-number-matched). Returns the ``AptStep`` or
    None."""
    for step in dockerfile_apt_steps(BACKEND_DOCKERFILE_REL):
        if BUG_SITE_TOKEN in step.packages:
            return step
    return None


def arg_declarations(dockerfile_text):
    """Every ``ARG`` instruction as ``(lineno, arg_name, before_first_from)``
    — the structural facts behind the ARG-scoping pin (case 5)."""
    decls = []
    seen_from = False
    for lineno, line in logical_lines(dockerfile_text):
        stripped = line.lstrip()
        if stripped.upper().startswith("FROM "):
            seen_from = True
            continue
        if stripped.upper().startswith("ARG "):
            name = stripped.split()[1].split("=")[0]
            decls.append((lineno, name, not seen_from))
    return decls
