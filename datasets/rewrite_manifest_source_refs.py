#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Rewrite the ``source-ref`` prefix of every line of a labeled manifest, so
the same labels point at another copy of the images (car-assembly-check
task 2.1, design §9): the Derived_Manifest of the grayscale variant is the
labeled manifest with ``.../assembly-photos/`` replaced by
``.../assembly-photos-monochrome/``.

Only the prefix changes: every other byte of a line is kept, the line
order is kept, and each rewritten line is re-parsed and compared with its
input (equal except ``source-ref``). Refused, exit 1, nothing written,
every offending line listed:

* a line that is not JSON;
* a line whose ``source-ref`` does not start with ``--from-prefix``;
* with ``--verify-dir``, a line whose relative key under ``--from-prefix``
  (``x.jpg``, or ``wrong-placements/x.jpg`` for a nested key) is not a file
  at that relative path under the directory.

Usage:
    python3 datasets/rewrite_manifest_source_refs.py \\
        --manifest output.manifest --out derived.manifest \\
        --from-prefix s3://bucket/assembly-photos/ \\
        --to-prefix s3://bucket/assembly-photos-monochrome/ \\
        --verify-dir /home/ubuntu/data/assembly-photos/monochrome

Exit codes: 0 written, 1 refused, 2 usage. Local files only; no AWS SDK.
"""
import argparse
import json
import re
import sys
from pathlib import Path
from typing import List, Optional, Tuple

_KEY_RE = re.compile(r'"source-ref"\s*:\s*"')


def rewrite_line(line: str, from_prefix: str, to_prefix: str) -> Tuple[Optional[str], str]:
    """``(new_line, relative_key)`` or ``(None, reason)``.

    The prefix is replaced in the line's own text, so every other byte is
    kept; the result is re-parsed and must equal the input object except
    for ``source-ref``.
    """
    try:
        entry = json.loads(line)
    except ValueError:
        return None, "not JSON"
    if not isinstance(entry, dict):
        return None, "not a JSON object"
    source_ref = entry.get("source-ref")
    if not isinstance(source_ref, str) or not source_ref.startswith(from_prefix):
        return None, "source-ref {0!r} does not start with {1!r}".format(source_ref, from_prefix)
    relative = source_ref[len(from_prefix):]
    expected = dict(entry)
    expected["source-ref"] = to_prefix + relative

    new_line = None
    encoded_from = json.dumps(from_prefix)[1:-1]
    encoded_to = json.dumps(to_prefix)[1:-1]
    matches = list(_KEY_RE.finditer(line))
    if len(matches) == 1 and line.startswith(encoded_from, matches[0].end()):
        start = matches[0].end()
        candidate = line[:start] + encoded_to + line[start + len(encoded_from):]
        try:
            if json.loads(candidate) == expected:
                new_line = candidate
        except ValueError:
            new_line = None
    if new_line is None:
        # The key is not written in its plain form (escaped characters):
        # re-serialize, which keeps the parsed object.
        new_line = json.dumps(expected, ensure_ascii=False)
    if json.loads(new_line) != expected:
        return None, "the rewritten line does not re-parse to the input object"
    return new_line, relative


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Rewrite the source-ref prefix of every manifest line "
                    "(nothing else changes; exit 1 and nothing written on any refusal).")
    ap.add_argument("--manifest", required=True, help="input manifest (JSON Lines)")
    ap.add_argument("--out", required=True, help="output manifest")
    ap.add_argument("--from-prefix", required=True)
    ap.add_argument("--to-prefix", required=True)
    ap.add_argument("--verify-dir",
                    help="every rewritten object's relative key must be a file under this directory")
    args = ap.parse_args(argv)

    try:
        text = Path(args.manifest).read_text(encoding="utf-8")
    except OSError as exc:
        print("error: cannot read {0}: {1}".format(args.manifest, exc), file=sys.stderr)
        return 1
    lines = [line for line in text.splitlines() if line.strip()]
    verify = Path(args.verify_dir) if args.verify_dir else None
    if verify is not None and not verify.is_dir():
        print("error: --verify-dir {0} is not a directory".format(verify), file=sys.stderr)
        return 1

    out_lines: List[str] = []
    refusals: List[str] = []
    for number, line in enumerate(lines, 1):
        new_line, detail = rewrite_line(line, args.from_prefix, args.to_prefix)
        if new_line is None:
            refusals.append("line {0}: {1}".format(number, detail))
            continue
        if verify is not None and not (verify / detail).is_file():
            refusals.append("line {0}: {1} is not a file under {2}".format(number, detail, verify))
            continue
        out_lines.append(new_line)

    print("lines: {0}, rewritten: {1}".format(len(lines), len(out_lines)))
    if refusals:
        print("refused ({0} lines; nothing written):".format(len(refusals)))
        for refusal in refusals:
            print("  " + refusal)
        return 1
    Path(args.out).write_text("".join(line + "\n" for line in out_lines), encoding="utf-8")
    print("wrote {0}".format(args.out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
