#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Fail when a tracked path cannot be checked out on Windows.

Git for Windows refuses paths that NTFS and the Win32 API cannot represent
(core.protectNTFS, on by default), and `git clone` then stops with
"error: invalid path ..." and "fatal: unable to checkout working tree".
Linux and macOS accept those paths, so nothing else catches them.

Usage, from the repository root (REV is any tree-ish, default HEAD):

    python3 .github/scripts/check_windows_paths.py [REV]

Every path in the tree (`git ls-tree -r -z --name-only`) must pass:

  1. No character that Git for Windows rejects: < > : " | ? * backslash,
     or a control character (0x01-0x1F).
  2. No path component that ends in a space or a period.
  3. No reserved device name as a path component, in any case, alone or
     followed by optional spaces and then an extension or ':': CON, PRN,
     AUX, NUL, COM1-COM9, LPT0-LPT9, CONIN$, CONOUT$. So aux.py, nul and
     "NUL .txt" are reserved; auxiliary.py, nullable.ts and com10.txt are not.
  4. No two paths that differ only by case, such as Readme.md and README.md,
     and no path that differs only by case from a directory, such as Foo
     alongside foo/bar. They collide on the default case-insensitive file
     systems of Windows and macOS. Two spellings of one directory, such as
     Docs/a.md and docs/b.md, are not flagged: they merge into one directory.
  5. No path longer than MAX_PATH_LENGTH characters.

Rules 1-3 mirror is_valid_win32_path() in Git's compat/mingw.c and
verify_path_internal() in read-cache.c, which also rejects the backslash on
Windows. Git treats LPT0 (but not COM0) as reserved, and so does this check.

Exit status: 0 when every path passes, 1 when any path fails, 2 when the
tree cannot be listed.
"""

import argparse
import os
import re
import subprocess
import sys
from collections import defaultdict

# Windows' MAX_PATH (260 characters, terminating NUL included) limits the
# whole path, including the directory the repository is cloned into, and Git
# for Windows does not enable core.longpaths by default. Capping repository
# paths at 200 leaves about 60 characters for the clone directory, e.g.
# C:\Users\<name>\source\repos\DefectDetectionApplication. Lengths are
# counted in UTF-16 code units, as Windows counts them.
MAX_PATH_LENGTH = 200

# Characters is_valid_win32_path() rejects, plus the backslash, which
# verify_path_internal() rejects on Windows.
INVALID_CHARACTERS = re.compile(r'[<>:"|?*\\\x01-\x1f]')

# A reserved device name, alone or followed by optional spaces and then '.'
# (an extension) or ':' (an alternate data stream). Git compares ASCII
# letters only, so re.ASCII stops re.IGNORECASE from also matching
# look-alikes such as U+0131 (dotless i).
RESERVED_NAME = re.compile(
    r"(CON|PRN|AUX|NUL|COM[1-9]|LPT[0-9]|CONIN\$|CONOUT\$) *(?:[.:]|\Z)",
    re.IGNORECASE | re.ASCII,
)


def path_problems(path):
    """Yield the reasons Windows cannot check out `path` (rules 1-3 and 5)."""
    characters = INVALID_CHARACTERS.findall(path)
    if characters:
        unique = ", ".join(ascii(c) for c in dict.fromkeys(characters))
        yield f"invalid character {unique}"

    # Git splits components on both separators.
    for component in re.split(r"[/\\]", path):
        # Git exempts "." and ".." here, and rejects them on every platform
        # elsewhere in verify_path().
        if component.endswith((" ", ".")) and component not in (".", ".."):
            ending = "a space" if component.endswith(" ") else "a period"
            yield f"component {ascii(component)} ends in {ending}"
        reserved = RESERVED_NAME.match(component)
        if reserved:
            name = reserved.group(1).upper()
            yield f"component {ascii(component)} is the reserved name {name}"

    length = len(path.encode("utf-16-le", "surrogatepass")) // 2
    if length > MAX_PATH_LENGTH:
        yield f"{length} characters long, over the {MAX_PATH_LENGTH} limit"


def case_collisions(paths):
    """
    Find the paths that collide on a case-insensitive file system (rule 4).

    Return two maps. The first maps each path to the other paths that differ
    from it only by case, such as README.md for Readme.md. The second maps
    each path to the directories that differ from it only by case, since a
    file and a directory can't share a name: foo for Foo when foo/bar is
    tracked, or A/foo for a/Foo when A/foo/x is. A directory is any leading
    chain of components of a path. Two spellings of one directory, such as
    Docs/a.md and docs/b.md, are in neither map: they merge into one directory.
    """
    # casefold() is slightly stricter than NTFS, which upper-cases one
    # character at a time (casefold() also pairs "ß" with "ss").
    groups = defaultdict(list)
    # Each spelling of each directory, keyed by casefold(), in first-seen order.
    directories = defaultdict(dict)
    for path in paths:
        groups[path.casefold()].append(path)
        # The directories of a/b/c are a/b and a.
        directory = path.rpartition("/")[0]
        while directory:
            directories[directory.casefold()][directory] = None
            directory = directory.rpartition("/")[0]
    collisions = {}
    for group in groups.values():
        if len(group) > 1:
            for path in group:
                collisions[path] = [other for other in group if other != path]
    directory_collisions = {}
    for path in paths:
        spellings = [other for other in directories.get(path.casefold(), ()) if other != path]
        if spellings:
            directory_collisions[path] = spellings
    return collisions, directory_collisions


def report(message):
    """Print one finding, as an error annotation when running in Actions."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        # Escape the command data the way @actions/core does (escapeData in
        # https://github.com/actions/toolkit/blob/main/packages/core/src/command.ts).
        message = message.replace("%", "%25").replace("\r", "%0D").replace("\n", "%0A")
        print(f"::error title=Windows path check::{message}")
    else:
        print(message)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Fail when a tracked path cannot be checked out on Windows."
    )
    parser.add_argument("rev", nargs="?", default="HEAD", help="tree-ish to check (default: HEAD)")
    args = parser.parse_args(argv)
    if args.rev.startswith("-"):
        parser.error(f"not a tree-ish: {ascii(args.rev)}")

    # -z: NUL-terminated names, never quoted or escaped by git.
    # --full-tree: list the whole tree even when run from a subdirectory.
    command = ["git", "ls-tree", "-r", "-z", "--name-only", "--full-tree", args.rev]
    try:
        listing = subprocess.run(command, stdout=subprocess.PIPE, check=False)
    except OSError as error:
        print(f"check_windows_paths: cannot run git: {error}", file=sys.stderr)
        return 2
    if listing.returncode != 0:
        print(
            f"check_windows_paths: cannot list the tree of {ascii(args.rev)}",
            file=sys.stderr,
        )
        return 2

    # Every path is printed with ascii(), which escapes control and non-ASCII
    # characters, so a file name cannot start a workflow command or garble
    # the log.
    paths = [
        name.decode("utf-8", "surrogateescape") for name in listing.stdout.split(b"\0") if name
    ]
    collisions, directory_collisions = case_collisions(paths)
    failed = 0
    for path in paths:
        problems = list(path_problems(path))
        if path in collisions:
            others = ", ".join(ascii(other) for other in collisions[path])
            problems.append(f"differs only by case from {others}")
        if path in directory_collisions:
            spellings = directory_collisions[path]
            noun = "directory" if len(spellings) == 1 else "directories"
            others = ", ".join(ascii(f"{spelling}/") for spelling in spellings)
            problems.append(f"differs only by case from {noun} {others}")
        for problem in problems:
            report(f"{ascii(path)}: {problem}")
        failed += bool(problems)

    if failed:
        print(
            f"{failed} of {len(paths)} tracked paths in {ascii(args.rev)} fail "
            "the Windows path checks. Rename them; the rules are listed at the "
            "top of .github/scripts/check_windows_paths.py."
        )
        return 1
    print(f"All {len(paths)} tracked paths in {ascii(args.rev)} pass the Windows path checks.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
