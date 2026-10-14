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
"""Shared helpers for the ``build-docker-save-stdout-failure`` spec tests.

Bug condition C(X): a ``docker save`` invocation site X in ``build-custom.sh``
that streams the image tar through the snap-confined docker CLI's redirected
stdout (``docker save <image> > <file>``), which fails with
``write /dev/stdout: bad file descriptor`` (EBADF) under the SSM
RunShellScript execution context, leaving a 0-byte tar and aborting the build
via ``set -e``. Concretely on the unfixed tree: the flask-app save (the
confirmed live failure, portal build job ``d844a5fb-81d5-4294-956d-d6d6ae1f000e``,
AMD64 dedicated, 2026-08-09) and the react-webapp save (the identical
pattern, unreached only because the flask-app save aborts first).

These helpers parse the shell scripts as TEXT only — no ``docker``,
``subprocess``, or shell-out anywhere in this package (the spec's
no-docker-in-tests validation constraint). Import-light so the tests run
under ``pytest ... --noconftest`` without pulling in the backend package,
mirroring the proven sibling pattern (``backend_jammy_pkgs``,
``edgemlsdk_pythondev``).

Comment/string awareness is load-bearing: the unfixed script's own comment
block (the lines explaining why the redirect form was adopted) contains the
literal text ``docker save --output`` — a naive grep would misclassify it as
an invocation site. The tokenizer therefore tracks shell single-quote,
double-quote, comment, and escape state across the whole file (single-quoted
strings may span lines, e.g. the in-image gate block's ``bash -c '...'``),
and only an UNQUOTED ``docker`` token in command position classifies as an
invocation site.

Save-form classification (design Fix Checking):
- ``STDOUT_REDIRECT`` — a real (non-comment, non-string) ``docker save``
  whose image tar goes through a shell stdout redirect (``>``/``>>``, with
  no ``--output``/``-o`` flag). The bug-condition form.
- ``OUTPUT_PARTIAL_MV`` — the fixed form: ``docker save --output``/``-o``
  writing to a ``.partial``-suffixed destination (the atomic ``mv`` pairing
  is asserted separately on the helper body).
- ``OTHER`` — anything else (total: unknown constructs classify OTHER,
  never crash, never silently classify as the fixed form).

Token discipline: ``--output`` is matched as a whole token (or ``--output=``
prefix) — it never matches a hypothetical ``--output-foo``; ``-o`` is exact.
All textual anchors are content matches, never line numbers (the fix shifts
later line numbers — sibling precedent), except the deliberate structural
pins on lines 2-3 (``set -e`` / ``set -o pipefail``), which precede the fix
region and cannot shift.
"""
import os
import re
from collections import namedtuple

# build_save_pkgs/ -> backend-test/ -> test/ -> repo root (3 up).
REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "..")
)

BUILD_CUSTOM_REL = "build-custom.sh"

# The four scanned neighbor scripts (bugfix.md scan) — the Req 2.2 class
# boundary: none contains any docker save/export invocation site.
NEIGHBOR_SCRIPT_RELS = (
    "scripts/portal-build-agent.sh",
    "publish-ecr-only.sh",
    "com.dda.InferenceUploader/build-and-publish.sh",
    "src/edgemlsdk/build.sh",
)

# Save-form classes (design Fix Checking pseudocode).
STDOUT_REDIRECT = "STDOUT_REDIRECT"
OUTPUT_PARTIAL_MV = "OUTPUT_PARTIAL_MV"
OTHER = "OTHER"

# The fixed-form shared helper (design Change 1).
HELPER_NAME = "save_image_tar"

# The two images and their exact ZIP_MEMBERS tar destinations (Req 3.2).
FLASK_IMAGE = "flask-app"
REACT_IMAGE = "react-webapp"
FLASK_TAR = "custom-build/$COMPONENT_NAME/flask-app.tar"
REACT_TAR = "custom-build/$COMPONENT_NAME/react-webapp.tar"
EXPECTED_UNFIXED_REDIRECT_SITES = frozenset(
    {(FLASK_IMAGE, FLASK_TAR), (REACT_IMAGE, REACT_TAR)}
)

# Integrity-guard size threshold literal (design Decision 3): 1 MiB.
SIZE_THRESHOLD_LITERAL = "1048576"

# The live-log grep anchor (unchanged line 360; job d844a5fb logged the
# EBADF immediately after this line).
LOG_ANCHOR_LINE = 'echo "save docker images as tarvballs"'

# The pre-zip transient-file cleanup argument and the zip exclusion glob
# (lines 385-421 region; added by build-fleet-execution-failures).
TMP_CLEANUP_ARG = "./custom-build/$COMPONENT_NAME/.tmp-*"
ZIP_EXCLUSION_GLOB = "*/.tmp-*"


def read_repo_file(rel_path):
    with open(os.path.join(REPO_ROOT, rel_path), encoding="utf-8") as fh:
        return fh.read()


def norm_path(path):
    """Normalize a shell path for comparison: strip a leading ``./``."""
    return path[2:] if path is not None and path.startswith("./") else path


# --------------------------------------------------------------------------- #
# Shell text scanning (TEXT only — comment/string/escape aware)
# --------------------------------------------------------------------------- #
# Character categories: "code" (bare shell code), "sq" (inside single
# quotes), "dq" (inside double quotes), "esc" (a backslash-escaped char).
# Quote delimiters and comment content are dropped at scan time.

_AnnotatedLine = namedtuple("_AnnotatedLine", "lineno pieces continued")


def _annotate_lines(text):
    """Scan ``text`` character by character, tracking shell quote/comment
    state ACROSS lines (single- and double-quoted strings may span physical
    lines; comments end at newline). Returns a list of _AnnotatedLine with
    ``pieces`` = [(char, category), ...] and ``continued`` = True when the
    line ends with an unquoted backslash (logical-line continuation)."""
    out = []
    state = "code"  # code | sq | dq
    for lineno, line in enumerate(text.split("\n"), start=1):
        pieces = []
        continued = False
        in_comment = False
        i = 0
        while i < len(line):
            ch = line[i]
            if in_comment:
                i += 1
                continue
            if state == "code":
                if ch == "'":
                    state = "sq"
                elif ch == '"':
                    state = "dq"
                elif ch == "\\":
                    if i + 1 < len(line):
                        pieces.append((line[i + 1], "esc"))
                        i += 1
                    else:
                        continued = True
                elif ch == "#" and (
                    not pieces
                    or pieces[-1][1] == "code"
                    and pieces[-1][0] in " \t;&|("
                ):
                    in_comment = True
                else:
                    pieces.append((ch, "code"))
            elif state == "sq":
                if ch == "'":
                    state = "code"
                else:
                    pieces.append((ch, "sq"))
            else:  # dq
                if ch == '"':
                    state = "code"
                elif ch == "\\" and i + 1 < len(line):
                    pieces.append((line[i + 1], "esc"))
                    i += 1
                else:
                    pieces.append((ch, "dq"))
            i += 1
        out.append(_AnnotatedLine(lineno, pieces, continued))
    return out


def logical_lines(text):
    """Reconstruct logical lines across unquoted backslash continuations.
    Returns [(start_lineno, pieces), ...]."""
    result = []
    pending = None
    pending_start = None
    for line in _annotate_lines(text):
        pieces = line.pieces
        if pending is not None:
            pieces = pending + [(" ", "code")] + pieces
            start = pending_start
        else:
            start = line.lineno
        if line.continued:
            pending = pieces
            pending_start = start
            continue
        result.append((start, pieces))
        pending = None
        pending_start = None
    if pending is not None:
        result.append((pending_start, pending))
    return result


Token = namedtuple("Token", "text quoted op")

_OPERATOR_CHARS = "><;&|()"


def tokenize(pieces):
    """Tokenize an annotated logical line into shell tokens. Whitespace in
    code state splits tokens; quoted/escaped content joins the current token
    and marks it quoted; operator characters in code state become op tokens
    (``&&``, ``||``, ``;``, ``|``, ``>``, ``>>``, ``2>``, ``(``, ``)``,
    ...) — a redirect immediately preceded by an attached fd digit string
    (e.g. ``2>``) is merged into one op token."""
    tokens = []
    cur = []
    cur_quoted = False

    def flush():
        nonlocal cur, cur_quoted
        if cur:
            tokens.append(Token("".join(cur), cur_quoted, False))
        cur = []
        cur_quoted = False

    i = 0
    n = len(pieces)
    while i < n:
        ch, cat = pieces[i]
        if cat == "code":
            if ch in " \t":
                flush()
            elif ch in _OPERATOR_CHARS:
                op = ch
                if ch in "><" and cur and not cur_quoted and "".join(cur).isdigit():
                    # attached fd digits: 2> , 1> , 2>> ...
                    op = "".join(cur) + ch
                    cur = []
                else:
                    flush()
                # greedy multi-char operators
                if i + 1 < n and pieces[i + 1][1] == "code":
                    nxt = pieces[i + 1][0]
                    if (ch == "&" and nxt == "&") or (
                        ch == "|" and nxt == "|"
                    ) or (ch == ">" and nxt == ">") or (ch == ";" and nxt == ";"):
                        op += nxt
                        i += 1
                tokens.append(Token(op, False, True))
            else:
                cur.append(ch)
        else:
            cur.append(ch)
            cur_quoted = True
        i += 1
    flush()
    return tokens


_SEGMENT_SPLIT_OPS = frozenset({"&&", "||", ";", ";;", "|", "(", ")", "&"})


def split_segments(tokens):
    """Split a token list into simple-command segments on control operators
    (``&&``, ``||``, ``;``, ``|``, parens)."""
    segments = []
    cur = []
    for tok in tokens:
        if tok.op and tok.text in _SEGMENT_SPLIT_OPS:
            if cur:
                segments.append(cur)
            cur = []
        else:
            cur.append(tok)
    if cur:
        segments.append(cur)
    return segments


_RESERVED_WORDS = frozenset(
    {"if", "!", "then", "else", "elif", "fi", "while", "until",
     "do", "done", "time", "{", "}"}
)
_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\+?=")


def command_index(segment):
    """Index of the command word in a simple-command segment: skips shell
    reserved words, variable-assignment prefixes, and leading redirects.
    Returns None when the segment has no command word."""
    i = 0
    while i < len(segment):
        tok = segment[i]
        if tok.op:
            # a leading redirect consumes its target
            i += 2 if tok.text.lstrip("0123456789").startswith(">") else 1
            continue
        if not tok.quoted and tok.text in _RESERVED_WORDS:
            i += 1
            continue
        if not tok.quoted and _ASSIGNMENT_RE.match(tok.text):
            i += 1
            continue
        return i
    return None


# --------------------------------------------------------------------------- #
# docker save / export invocation-site scan and save-form classification
# --------------------------------------------------------------------------- #
Site = namedtuple("Site", "lineno image form dest text")


def _segment_text(segment):
    return " ".join(t.text for t in segment)


def _classify_docker_segment(segment, lineno, subcommands):
    """Classify one simple-command segment. Returns a Site when the segment
    is a real (non-comment, non-string) ``docker <subcommand>`` invocation,
    else None. Total: unknown shapes classify OTHER, never raise."""
    ci = command_index(segment)
    if ci is None:
        return None
    cmd = segment[ci]
    if cmd.quoted or cmd.text != "docker":
        return None
    if ci + 1 >= len(segment):
        return None
    sub = segment[ci + 1]
    if sub.op or sub.quoted or sub.text not in subcommands:
        return None
    output_dest = None
    redirect = None
    image = None
    i = ci + 2
    while i < len(segment):
        tok = segment[i]
        if tok.op:
            if tok.text in (">", ">>", "1>", "1>>"):
                if i + 1 < len(segment) and not segment[i + 1].op:
                    redirect = segment[i + 1].text
                    i += 1
            elif tok.text in ("2>", "2>>"):
                i += 1  # skip the stderr target
        elif not tok.quoted and tok.text in ("--output", "-o"):
            if i + 1 < len(segment) and not segment[i + 1].op:
                output_dest = segment[i + 1].text
                i += 1
        elif not tok.quoted and tok.text.startswith("--output="):
            output_dest = tok.text[len("--output="):]
        elif not tok.quoted and tok.text.startswith("-"):
            pass  # some other flag — never treated as --output (token discipline)
        else:
            if image is None:
                image = tok.text
        i += 1
    if output_dest is not None:
        form = OUTPUT_PARTIAL_MV if output_dest.endswith(".partial") else OTHER
        dest = output_dest
    elif redirect is not None:
        form = STDOUT_REDIRECT
        dest = redirect
    else:
        form = OTHER
        dest = None
    return Site(lineno, image, form, dest, _segment_text(segment))


def docker_sites(text, subcommands=("save",)):
    """Every real ``docker <subcommand>`` invocation site in ``text`` as a
    Site, in file order. Comment/string content never classifies. Total:
    a segment that cannot be parsed classifies OTHER rather than raising."""
    sites = []
    for lineno, pieces in logical_lines(text):
        for segment in split_segments(tokenize(pieces)):
            try:
                site = _classify_docker_segment(segment, lineno, subcommands)
            except Exception:  # totality: never crash on odd shell shapes
                site = Site(lineno, None, OTHER, None, _segment_text(segment))
            if site is not None:
                sites.append(site)
    return sites


def stdout_redirect_sites(text):
    """The bug-condition scan: every docker save site of form
    STDOUT_REDIRECT."""
    return [s for s in docker_sites(text) if s.form == STDOUT_REDIRECT]


def save_or_export_sites(text):
    """Class-boundary scan: every docker save OR docker export invocation
    site (used over the neighbor scripts)."""
    return docker_sites(text, subcommands=("save", "export"))


def format_sites(rel_path, sites):
    return "\n".join(
        f"  {rel_path}:{s.lineno}: form={s.form} image={s.image!r} "
        f"dest={s.dest!r} in: {s.text}"
        for s in sites
    )


# --------------------------------------------------------------------------- #
# Fixed-form helper (save_image_tar) — call sites and body structure
# --------------------------------------------------------------------------- #
def helper_call_sites(text, helper_name=HELPER_NAME):
    """Every simple-command segment invoking ``helper_name`` as its command
    word. Returns [(lineno, [arg texts]), ...]. The function DEFINITION line
    (``save_image_tar() {``) tokenizes as ``save_image_tar`` + ``(`` and is
    split apart by the paren operators, so a definition never counts as a
    call site; call sites have arguments, definitions do not — we require
    at least one argument."""
    calls = []
    for lineno, pieces in logical_lines(text):
        for segment in split_segments(tokenize(pieces)):
            ci = command_index(segment)
            if ci is None:
                continue
            cmd = segment[ci]
            if cmd.quoted or cmd.text != helper_name:
                continue
            args = [t.text for t in segment[ci + 1:] if not t.op]
            if args:
                calls.append((lineno, args))
    return calls


_HELPER_DEF_RE = re.compile(
    r"^\s*" + re.escape(HELPER_NAME) + r"\s*\(\s*\)\s*\{"
)


def find_helper_body(text):
    """Locate the ``save_image_tar() {`` function definition and return
    ``(start_lineno, body_text)`` — the text between the opening and the
    matching closing brace (brace depth counted over code-state characters
    only). Returns None when no helper exists (the unfixed tree)."""
    annotated = _annotate_lines(text)
    raw_lines = text.split("\n")
    for idx, line in enumerate(annotated):
        code_text = "".join(ch for ch, cat in line.pieces if cat == "code")
        if not _HELPER_DEF_RE.match(raw_lines[idx]) and not _HELPER_DEF_RE.match(
            code_text
        ):
            continue
        depth = 0
        body_lines = []
        for j in range(idx, len(annotated)):
            for ch, cat in annotated[j].pieces:
                if cat != "code":
                    continue
                if ch == "{":
                    depth += 1
                elif ch == "}":
                    depth -= 1
            if j > idx:
                body_lines.append(raw_lines[j])
            if j > idx and depth == 0:
                # drop the closing-brace line itself
                return line.lineno, "\n".join(body_lines[:-1])
        return line.lineno, "\n".join(body_lines)
    return None


def mv_commands(text):
    """Every ``mv`` simple command as (lineno, [arg texts])."""
    moves = []
    for lineno, pieces in logical_lines(text):
        for segment in split_segments(tokenize(pieces)):
            ci = command_index(segment)
            if ci is None:
                continue
            cmd = segment[ci]
            if cmd.quoted or cmd.text != "mv":
                continue
            moves.append(
                (lineno, [t.text for t in segment[ci + 1:] if not t.op])
            )
    return moves


# --------------------------------------------------------------------------- #
# Zip-side guards (Req 2.3) and structural anchors
# --------------------------------------------------------------------------- #
def zip_members(text):
    """Parse the explicit ``ZIP_MEMBERS=( ... )`` array: returns the member
    strings (quotes stripped) or None when the array is absent."""
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip().startswith("ZIP_MEMBERS=("):
            members = []
            for j in range(i + 1, len(lines)):
                stripped = lines[j].strip()
                if stripped == ")":
                    return members
                if stripped.startswith("#") or not stripped:
                    continue
                if (
                    len(stripped) >= 2
                    and stripped[0] == stripped[-1]
                    and stripped[0] in "\"'"
                ):
                    stripped = stripped[1:-1]
                members.append(stripped)
            return members
    return None


def has_tmp_cleanup(text):
    """True iff a pre-zip ``rm -f ./custom-build/$COMPONENT_NAME/.tmp-*``
    cleanup command exists."""
    for _, pieces in logical_lines(text):
        for segment in split_segments(tokenize(pieces)):
            ci = command_index(segment)
            if ci is None:
                continue
            cmd = segment[ci]
            if cmd.quoted or cmd.text != "rm":
                continue
            args = [t.text for t in segment[ci + 1:] if not t.op]
            if "-f" in args and TMP_CLEANUP_ARG in args:
                return True
    return False


def zip_exclusion_present(text):
    """True iff a ``zip`` invocation carries ``-x`` with the ``*/.tmp-*``
    exclusion glob (whole-token match)."""
    for _, pieces in logical_lines(text):
        for segment in split_segments(tokenize(pieces)):
            ci = command_index(segment)
            if ci is None:
                continue
            cmd = segment[ci]
            if cmd.quoted or cmd.text != "zip":
                continue
            for k in range(ci + 1, len(segment) - 1):
                if (
                    not segment[k].op
                    and not segment[k].quoted
                    and segment[k].text == "-x"
                    and segment[k + 1].text == ZIP_EXCLUSION_GLOB
                ):
                    return True
    return False


def zip_uses_explicit_member_list(text):
    """True iff the zip invocation packages the explicit ``ZIP_MEMBERS``
    expansion rather than a recursive directory scan."""
    for _, pieces in logical_lines(text):
        for segment in split_segments(tokenize(pieces)):
            ci = command_index(segment)
            if ci is None:
                continue
            cmd = segment[ci]
            if cmd.quoted or cmd.text != "zip":
                continue
            if any("ZIP_MEMBERS[@]" in t.text for t in segment[ci + 1:]):
                return True
    return False


def has_verbatim_line(text, wanted):
    """Content anchor: True iff some physical line strips to exactly
    ``wanted``."""
    return any(line.strip() == wanted for line in text.split("\n"))
