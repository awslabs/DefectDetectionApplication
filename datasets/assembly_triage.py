#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
The shared reader of the owner-confirmed Triage_File (car-assembly-check
design DD18): every script that takes ``--triage`` imports it, so the
Known_Good / Fail_Set / Occluded_Correct split and the per-row piece
counts are read one way everywhere.

The Triage_File (``placement-triage.csv``) has a header row and the eleven
columns of ``REQUIRED_COLUMNS``, one row per image of the Labeled_Set.
Columns are read by name; ``filename`` is the join key (the basename of a
manifest ``source-ref`` or of a Result_Export record's ``file``).

Set rules (DD18 with owner decisions O15 (i) and O19):

* **Known_Good** iff ``verdict`` is in ``KNOWN_GOOD_VERDICTS``, ``occluder``
  is in ``NO_OCCLUDER_VALUES`` (case-insensitive, stripped; the file writes
  the literal ``none``) and (``STRAY_ROWS_ARE_KNOWN_GOOD`` or
  ``unlabeled_stray == 0``). With the constant ``True`` a stray piece
  outside the car outline excludes no row, because the Layout Check ignores
  such a detection at runtime.
* **Fail_Set** iff ``misplaced_pieces`` or ``missing_pieces`` is non-empty.
* **Occluded_Correct** otherwise: no verdict expectation.

``piece_counts`` is parsed against ``PIECE_COUNTS_RE`` and nothing else is
assumed about it: it is never derived from or checked against ``verdict``,
``misplaced_pieces`` or ``missing_pieces``. ``set``, ``source_path``,
``confidence`` and ``notes`` are printed, never interpreted.

Anything the reader cannot read as written is listed in ``problems`` with
the row's ``confidence`` and ``notes``, never guessed: an unrecognised
verdict, a piece name outside ``PIECE_CLASSES``, a duplicate filename, a
malformed ``piece_counts`` cell, an ``unlabeled_stray`` that is not a
non-negative integer, and a file without the eleven columns (the
nine-column ``.v1`` backup is refused as missing ``piece_counts`` and
``unlabeled_stray``). ``read_triage`` never raises on content.

As a script it prints the three sets and every listing, joined against the
images of the given manifests and Result_Exports, and exits 1 on any
listing:

    python3 datasets/assembly_triage.py --triage placement-triage.csv \\
        [--manifest output.manifest]... [--results gray.json]...

Standard library only; no AWS SDK.
"""
import argparse
import csv
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Set, Tuple

# Vocabulary pinned from the confirmed file (DD18).
KNOWN_GOOD_VERDICTS = ("correct",)
RECOGNISED_VERDICTS = ("correct", "incorrect", "incorrect_and_missing", "missing")
NO_OCCLUDER_VALUES = ("", "none")
PIECE_CLASSES = ("front_fender", "rear_fender", "roof")
PIECE_COUNTS_RE = re.compile(r"^front_fender=(\d+);rear_fender=(\d+);roof=(\d+)$")
REQUIRED_COLUMNS = (
    "filename", "set", "source_path", "verdict", "misplaced_pieces",
    "missing_pieces", "occluder", "confidence", "notes", "piece_counts",
    "unlabeled_stray",
)
#: Owner decision O19 (design DD23): a stray piece outside the car outline
#: is ignored at runtime, so it excludes no row from the Known_Good_Set.
#: ``False`` restores O15 (i)'s earlier rule and belongs only to a
#: deployment with ``min_outline_overlap = 0``.
STRAY_ROWS_ARE_KNOWN_GOOD = True

SET_KNOWN_GOOD = "Known_Good"
SET_FAIL = "Fail_Set"
SET_OCCLUDED_CORRECT = "Occluded_Correct"

_PIECE_SPLIT_RE = re.compile(r"[,;\s]+")


@dataclass(frozen=True)
class TriageRow:
    filename: str
    set: str
    verdict: str
    misplaced: Tuple[str, ...]
    missing: Tuple[str, ...]
    occluder: str
    confidence: str
    notes: str
    piece_counts: Dict[str, int]
    unlabeled_stray: int
    source_path: str = ""

    @property
    def has_occluder(self) -> bool:
        return self.occluder.strip().lower() not in NO_OCCLUDER_VALUES

    def describe(self) -> str:
        """The row as one line, for every listing that quotes it."""
        return ("{0}: set={1} verdict={2} misplaced={3} missing={4} "
                "occluder={5} piece_counts={6} unlabeled_stray={7} "
                "confidence={8} notes={9!r}").format(
                    self.filename, self.set, self.verdict,
                    ";".join(self.misplaced) or "-",
                    ";".join(self.missing) or "-",
                    self.occluder or "-", _counts_text(self.piece_counts),
                    self.unlabeled_stray, self.confidence, self.notes)


@dataclass
class Triage:
    rows: Dict[str, TriageRow]
    known_good: Set[str]
    fail: Set[str]
    occluded_correct: Set[str]
    problems: List[str]

    def set_of(self, filename: str) -> Optional[str]:
        """``Known_Good``, ``Fail_Set``, ``Occluded_Correct`` or ``None``."""
        if filename in self.known_good:
            return SET_KNOWN_GOOD
        if filename in self.fail:
            return SET_FAIL
        if filename in self.occluded_correct:
            return SET_OCCLUDED_CORRECT
        return None

    def expected_pieces(self, filename: str) -> Set[str]:
        """The row's ``misplaced ∪ missing`` (empty for an unknown file)."""
        row = self.rows.get(filename)
        if row is None:
            return set()
        return set(row.misplaced) | set(row.missing)

    def multiplicity_rule(self, filename: str) -> Dict[str, int]:
        """The row's exact ``piece_counts`` per class (Requirement 1.4);
        empty for an unknown file or a cell that did not parse."""
        row = self.rows.get(filename)
        if row is None:
            return {}
        return dict(row.piece_counts)

    def join(self, filenames: Iterable[str]) -> Tuple[Set[str], Set[str], Set[str]]:
        """``(matched, images_without_row, rows_without_image)``."""
        names = set(filenames)
        matched = names & set(self.rows)
        return matched, names - matched, set(self.rows) - names


def _counts_text(counts: Dict[str, int]) -> str:
    if not counts:
        return "-"
    return ";".join("{0}={1}".format(c, counts[c]) for c in PIECE_CLASSES if c in counts)


def _pieces(cell: str) -> Tuple[str, ...]:
    return tuple(token for token in _PIECE_SPLIT_RE.split(cell.strip()) if token)


def _quote(line_no: int, filename: str, confidence: str, notes: str) -> str:
    return "row {0} ({1}) [confidence={2}, notes={3!r}]".format(
        line_no, filename or "<no filename>", confidence, notes)


def read_triage(path: str) -> Triage:
    """Read the Triage_File at ``path``; never raises on content."""
    triage = Triage(rows={}, known_good=set(), fail=set(),
                    occluded_correct=set(), problems=[])
    try:
        with open(path, "r", encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            header = list(reader.fieldnames or [])
            missing_columns = [c for c in REQUIRED_COLUMNS if c not in header]
            if missing_columns:
                triage.problems.append(
                    "{0}: missing column(s) {1} (the eleven columns {2} are "
                    "required)".format(path, ", ".join(missing_columns),
                                       ", ".join(REQUIRED_COLUMNS)))
                return triage
            raw_rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        triage.problems.append("{0}: cannot read the Triage_File: {1}".format(path, exc))
        return triage

    for index, raw in enumerate(raw_rows):
        line_no = index + 2  # the header is line 1
        values = {c: (raw.get(c) or "").strip() for c in REQUIRED_COLUMNS}
        filename = values["filename"]
        quote = _quote(line_no, filename, values["confidence"], values["notes"])
        if not filename:
            triage.problems.append("{0}: empty filename".format(quote))
            continue
        if filename in triage.rows:
            triage.problems.append("{0}: duplicate filename (the first row is kept)".format(quote))
            continue
        row_problems = []
        verdict = values["verdict"]
        if verdict not in RECOGNISED_VERDICTS:
            row_problems.append("unrecognised verdict {0!r} (recognised: {1})".format(
                verdict, ", ".join(RECOGNISED_VERDICTS)))
        misplaced = _pieces(values["misplaced_pieces"])
        missing = _pieces(values["missing_pieces"])
        for name in misplaced + missing:
            if name not in PIECE_CLASSES:
                row_problems.append("piece name {0!r} is not a Piece_Class ({1})".format(
                    name, ", ".join(PIECE_CLASSES)))
        match = PIECE_COUNTS_RE.match(values["piece_counts"])
        piece_counts: Dict[str, int] = {}
        if match:
            piece_counts = {c: int(v) for c, v in zip(PIECE_CLASSES, match.groups())}
        else:
            row_problems.append("piece_counts {0!r} does not match {1}".format(
                values["piece_counts"], PIECE_COUNTS_RE.pattern))
        stray_text = values["unlabeled_stray"]
        unlabeled_stray = 0
        if stray_text.isdigit() and stray_text.isascii():
            unlabeled_stray = int(stray_text)
        else:
            row_problems.append("unlabeled_stray {0!r} is not a non-negative integer".format(
                stray_text))
        for problem in row_problems:
            triage.problems.append("{0}: {1}".format(quote, problem))

        row = TriageRow(
            filename=filename, set=values["set"], verdict=verdict,
            misplaced=misplaced, missing=missing, occluder=values["occluder"],
            confidence=values["confidence"], notes=values["notes"],
            piece_counts=piece_counts, unlabeled_stray=unlabeled_stray,
            source_path=values["source_path"])
        triage.rows[filename] = row
        if verdict not in RECOGNISED_VERDICTS:
            continue  # listed; never placed in a set by guess
        if misplaced or missing:
            triage.fail.add(filename)
        elif (verdict in KNOWN_GOOD_VERDICTS and not row.has_occluder
              and (STRAY_ROWS_ARE_KNOWN_GOOD or unlabeled_stray == 0)):
            triage.known_good.add(filename)
        else:
            triage.occluded_correct.add(filename)
    return triage


def load_test_files(value: Optional[str]) -> Optional[Set[str]]:
    """``--test-files``: a file with one name per line, else a
    comma-separated list of names; basenames are kept."""
    if not value:
        return None
    path = Path(value)
    text = path.read_text(encoding="utf-8") if path.is_file() else value.replace(",", "\n")
    return {Path(line.strip()).name for line in text.splitlines() if line.strip()}


def _manifest_filenames(path: str, problems: List[str]) -> List[str]:
    names = []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        problems.append("{0}: cannot read: {1}".format(path, exc))
        return names
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            problems.append("{0} line {1}: not JSON".format(path, number))
            continue
        source_ref = entry.get("source-ref") if isinstance(entry, dict) else None
        if not isinstance(source_ref, str) or not source_ref:
            problems.append("{0} line {1}: no source-ref".format(path, number))
            continue
        names.append(source_ref.rsplit("/", 1)[-1])
    return names


def _results_filenames(path: str, problems: List[str]) -> List[str]:
    try:
        export = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        problems.append("{0}: cannot read the Result_Export: {1}".format(path, exc))
        return []
    records = export.get("records") if isinstance(export, dict) else None
    if not isinstance(records, list):
        problems.append("{0}: no records list".format(path))
        return []
    names = []
    for index, record in enumerate(records):
        name = record.get("file") if isinstance(record, dict) else None
        if not isinstance(name, str) or not name:
            problems.append("{0} record {1}: no file".format(path, index))
            continue
        names.append(Path(name).name)
    return names


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Read the Triage_File, print the Known_Good / Fail_Set / "
                    "Occluded_Correct sets and every listing (exit 1 on any).")
    ap.add_argument("--triage", required=True, help="placement-triage.csv (eleven columns)")
    ap.add_argument("--manifest", action="append", default=[],
                    help="a labeled manifest whose images are joined (repeatable)")
    ap.add_argument("--results", action="append", default=[],
                    help="a Result_Export whose records are joined (repeatable)")
    args = ap.parse_args(argv)

    triage = read_triage(args.triage)
    print("triage: {0} ({1} rows)".format(args.triage, len(triage.rows)))
    for label, members in ((SET_KNOWN_GOOD, triage.known_good), (SET_FAIL, triage.fail),
                           (SET_OCCLUDED_CORRECT, triage.occluded_correct)):
        print("{0}: {1}".format(label, len(members)))
        for name in sorted(members):
            print("  " + triage.rows[name].describe())

    listings = 0
    if triage.problems:
        print("triage problems ({0}):".format(len(triage.problems)))
        for problem in triage.problems:
            print("  " + problem)
        listings += len(triage.problems)

    for kind, paths, reader in (("manifest", args.manifest, _manifest_filenames),
                                ("results", args.results, _results_filenames)):
        for path in paths:
            read_problems: List[str] = []
            names = reader(path, read_problems)
            _, without_row, without_image = triage.join(names)
            print("{0} {1}: {2} images".format(kind, path, len(names)))
            for problem in read_problems:
                print("  " + problem)
            for name in sorted(without_row):
                print("  image without a triage row: {0}".format(name))
            for name in sorted(without_image):
                row = triage.rows[name]
                print("  triage row without an image: {0} [confidence={1}, notes={2!r}]".format(
                    name, row.confidence, row.notes))
            listings += len(read_problems) + len(without_row) + len(without_image)

    print("listings: {0}".format(listings))
    return 1 if listings else 0


if __name__ == "__main__":
    sys.exit(main())
