#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Check the car-assembly-check manifests before training (task 2.2; design
§9, DD13, DD18; Requirements 1.4, 2.2, 2.3, 11.2).

    python3 datasets/check_assembly_manifests.py --labeled output.manifest \\
        [--labeled second.manifest] [--derived derived.manifest] \\
        [--triage placement-triage.csv] [--images-mono dir] [--images-color dir] \\
        [--classes front_fender,rear_fender,roof] [--expect-lines 70]

Checks, each failure listed with its line or filename:

* **Shape** (Requirement 1.4) on the labeled manifest(s), concatenated in
  the given order: the line count is ``--expect-lines``; every line is an
  Instance_Manifest line with ``image_size == [{W, H, 3}]`` (``--image-size``,
  default 4608x3288); every ``class_id`` is in the class-map's index set
  (``dda_manifest._parse_instance_segmentation``) and the class-map names
  exactly ``--classes``; every ``rle`` sums to W x H; ``source-ref`` starts
  with ``--source-prefix``; an instance below 0.5 % of the frame is listed.
  A line with zero annotations is accepted (the empty tray is a negative).
* **Counts** (only with ``--triage``): for every image and every class the
  number of annotations equals the row's ``piece_counts``
  (``Triage.multiplicity_rule``), exactly, never a rule derived from the
  other columns; each disagreement quotes the row's ``confidence`` and
  ``notes`` -- the script cannot tell whether the label or the row is
  wrong. Without ``--triage`` no count rule is applied. Plus the join
  problems and every problem of the reader; the three set sizes are
  printed, never pinned.
* **Derived** (with ``--derived``): line for line, the derived line was
  rewritten from the labeled line (the objects equal except ``source-ref``,
  same basename) and the parsed annotations are identical (Requirement 2.2).
  With two ``--labeled`` manifests this is the check that their
  concatenation in the given order is what ``--derived`` came from.
* **Split replay** (with ``--images-mono``; DD13): the converter's own
  sequence -- ``parse_entries`` -> ``group_by_similarity`` (threshold 6.0)
  -> ``split_groups`` (0.15 / 0.15, manifest order) -- asserting non-empty
  ``train`` / ``valid``, printing the groups, split sizes and ``test``
  filenames, and with ``--triage`` the Known_Good / Fail_Set count per
  split, stopping when ``train`` holds no Fail_Set image (Requirement 2.3).
  With ``--images-color`` too, the per-filename split must be identical on
  both image sets (Requirement 11.2).

Exit codes: 0 every check holds, 1 any failure (the full report is still
printed), 2 usage. Local files only; no AWS SDK.
"""
import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Dict, List, Optional, Tuple

_DATASETS = Path(__file__).resolve().parent
_SHARED_LAYER = _DATASETS.parent / "edge-cv-portal" / "backend" / "layers" / "shared" / "python"
for _path in (str(_DATASETS), str(_SHARED_LAYER)):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import assembly_triage  # noqa: E402
import dda_manifest  # noqa: E402
import manifest_to_detector_dataset as conv  # noqa: E402

DEFAULT_SOURCE_PREFIX = "s3://dda-usecase-reinvent-326334468650-us-west-2/assembly-photos/"
DEFAULT_IMAGE_SIZE = "4608x3288"
DEFAULT_CLASSES = ",".join(assembly_triage.PIECE_CLASSES)
MIN_INSTANCE_FRACTION = 0.005
GROUP_THRESHOLD = 6.0
VAL_FRAC = 0.15
TEST_FRAC = 0.15
#: The converter's split names, as RF-DETR's layout prints them.
SPLIT_NAMES = (("train", "train"), ("val", "valid"), ("test", "test"))


def read_lines(path: str) -> List[str]:
    text = Path(path).read_text(encoding="utf-8")
    return [line for line in text.splitlines() if line.strip()]


def parse_size(value: str) -> Tuple[int, int]:
    try:
        width, height = (int(part) for part in value.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError("expected WxH, got {0!r}".format(value))
    if width < 1 or height < 1:
        raise argparse.ArgumentTypeError("expected a positive WxH, got {0!r}".format(value))
    return width, height


def _instance_entry(entry: dict) -> Optional[dict]:
    """The line in the literal ``instance-segmentation`` form, or None."""
    attr = conv.detect_instance_attribute(entry)
    if attr is None or not isinstance(entry.get(attr), dict):
        return None
    return {
        "source-ref": entry.get("source-ref"),
        "instance-segmentation": entry[attr],
        "instance-segmentation-metadata": entry.get(attr + "-metadata") or {},
    }


def shape_checks(lines: List[str], size: Tuple[int, int], prefix: str,
                 classes: List[str]) -> Tuple[List[dict], List[str]]:
    """Per-line infos ``{number, file, labels: [class names], parsed}`` and
    the shape failures."""
    width, height = size
    failures: List[str] = []
    infos: List[dict] = []
    for number, line in enumerate(lines, 1):
        where = "line {0}".format(number)
        try:
            raw = json.loads(line)
        except ValueError:
            failures.append("{0}: not JSON".format(where))
            continue
        source_ref = raw.get("source-ref") if isinstance(raw, dict) else None
        if not isinstance(source_ref, str) or not source_ref:
            failures.append("{0}: no source-ref".format(where))
            continue
        name = source_ref.rsplit("/", 1)[-1]
        where = "line {0} ({1})".format(number, name)
        if not source_ref.startswith(prefix):
            failures.append("{0}: source-ref {1!r} is not under {2}".format(where, source_ref, prefix))
        entry = _instance_entry(raw)
        if entry is None:
            failures.append("{0}: not an instance-segmentation line".format(where))
            continue
        image_size = entry["instance-segmentation"].get("image_size")
        if image_size != [{"width": width, "height": height, "depth": 3}]:
            failures.append("{0}: image_size {1!r} is not [{{width: {2}, height: {3}, depth: 3}}]".format(
                where, image_size, width, height))
            continue
        class_map = entry["instance-segmentation-metadata"].get("class-map") or {}
        if sorted(class_map.values()) != sorted(classes):
            failures.append("{0}: class-map {1!r} does not name exactly {2}".format(
                where, class_map, ", ".join(classes)))
        try:
            parsed = dda_manifest._parse_instance_segmentation(entry)
        except (KeyError, TypeError, ValueError) as exc:
            failures.append("{0}: {1}".format(where, exc))
            continue
        labels = []
        line_ok = True
        for index, instance in enumerate(parsed["annotation"]["instances"]):
            try:
                counts = dda_manifest._rle_counts(instance["rle"], width, height)
            except ValueError as exc:
                failures.append("{0} instance {1} ({2}): {3}".format(where, index, instance["class"], exc))
                line_ok = False
                continue
            area = sum(counts[1::2])
            if area < MIN_INSTANCE_FRACTION * width * height:
                failures.append("{0} instance {1} ({2}): {3} pixels, below 0.5 % of the frame".format(
                    where, index, instance["class"], area))
            labels.append(instance["class"])
        infos.append({"number": number, "file": name, "labels": labels,
                      "parsed": parsed["annotation"], "ok": line_ok})
    return infos, failures


def count_checks(infos: List[dict], triage) -> List[str]:
    """The exact per-row count rule of Requirement 1.4."""
    failures = []
    for info in infos:
        row = triage.rows.get(info["file"])
        rule = triage.multiplicity_rule(info["file"])
        if row is None or not rule:
            continue  # a join problem or a reader problem, listed on its own
        counts = Counter(info["labels"])
        wrong = []
        for label in sorted(set(rule) | set(counts)):
            expected = rule.get(label, 0)
            if counts.get(label, 0) != expected:
                wrong.append("{0}: {1} labeled, piece_counts says {2}".format(
                    label, counts.get(label, 0), expected))
        if wrong:
            failures.append(
                "line {0} ({1}): {2} [verdict={3}, piece_counts={4}, confidence={5}, notes={6!r}] "
                "-- cannot tell whether the label or the triage row is wrong".format(
                    info["number"], info["file"], "; ".join(wrong), row.verdict,
                    assembly_triage._counts_text(row.piece_counts), row.confidence, row.notes))
    return failures


def derived_checks(labeled: List[str], derived: List[str]) -> List[str]:
    """Line for line: rewritten from, and annotation-identical to, the
    labeled concatenation."""
    failures = []
    if len(labeled) != len(derived):
        failures.append("--derived has {0} lines, the labeled manifest(s) {1}".format(
            len(derived), len(labeled)))
    for number, (left, right) in enumerate(zip(labeled, derived), 1):
        try:
            a, b = json.loads(left), json.loads(right)
        except ValueError:
            failures.append("line {0}: not JSON on one side".format(number))
            continue
        ref_a, ref_b = str(a.get("source-ref")), str(b.get("source-ref"))
        if ref_a.rsplit("/", 1)[-1] != ref_b.rsplit("/", 1)[-1]:
            failures.append("line {0}: {1} vs {2}: different images".format(number, ref_a, ref_b))
        rest_a = {k: v for k, v in a.items() if k != "source-ref"}
        rest_b = {k: v for k, v in b.items() if k != "source-ref"}
        if rest_a != rest_b:
            failures.append("line {0}: the derived line differs beyond source-ref".format(number))
        entry_a, entry_b = _instance_entry(a), _instance_entry(b)
        try:
            ann_a = dda_manifest._parse_instance_segmentation(entry_a)["annotation"] if entry_a else None
            ann_b = dda_manifest._parse_instance_segmentation(entry_b)["annotation"] if entry_b else None
        except (KeyError, TypeError, ValueError) as exc:
            failures.append("line {0}: cannot parse the annotations: {1}".format(number, exc))
            continue
        if ann_a != ann_b:
            failures.append("line {0}: the parsed annotations differ (Requirement 2.2)".format(number))
    return failures


def replay_split(lines: List[str], images_dir: str) -> Tuple[Dict[str, str], int, List[str]]:
    """The converter's split: ``({file: split}, groups, failures)``."""
    records, _, _ = conv.parse_entries(lines)
    failures = ["{0}: not found under {1}".format(r["file_name"], images_dir)
                for r in records if not (Path(images_dir) / r["file_name"]).is_file()]
    groups, n_groups = conv.group_by_similarity(records, images_dir, GROUP_THRESHOLD)
    assigned = conv.split_groups(records, groups, VAL_FRAC, TEST_FRAC)
    membership = {}
    for split, recs in assigned.items():
        for rec in recs:
            membership[rec["file_name"]] = split
    return membership, n_groups, failures


def report_split(label: str, membership: Dict[str, str], n_groups: int, triage) -> List[str]:
    failures = []
    print("split replay ({0}): {1} groups".format(label, n_groups))
    for key, name in SPLIT_NAMES:
        members = sorted(f for f, s in membership.items() if s == key)
        line = "  {0}: {1}".format(name, len(members))
        if triage is not None:
            known_good = sum(1 for f in members if f in triage.known_good)
            fail = sum(1 for f in members if f in triage.fail)
            line += " (Known_Good {0}, Fail_Set {1})".format(known_good, fail)
            if key == "train" and fail == 0:
                failures.append("{0}: train holds no Fail_Set image -- stop (DD13, Requirement 2.3); "
                                "the remedy is the owner's".format(label))
        print(line)
        if key in ("train", "val") and not members:
            failures.append("{0}: the {1} split is empty".format(label, name))
        if key == "test":
            for f in members:
                suffix = ""
                if triage is not None:
                    suffix = " [{0}]".format(triage.set_of(f) or "no triage row")
                print("    {0}{1}".format(f, suffix))
    return failures


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="car-assembly-check manifest checks: shape, triage counts, "
                    "derived identity and the converter's split replay.")
    ap.add_argument("--labeled", action="append", required=True,
                    help="a labeled manifest (repeatable; concatenated in the given order)")
    ap.add_argument("--derived", help="the Derived_Manifest rewritten from the labeled one(s)")
    ap.add_argument("--triage", help="placement-triage.csv (eleven columns)")
    ap.add_argument("--images-mono", help="local directory with the grayscale images")
    ap.add_argument("--images-color", help="local directory with the colour images")
    ap.add_argument("--classes", default=DEFAULT_CLASSES)
    ap.add_argument("--expect-lines", type=int, default=70)
    ap.add_argument("--image-size", type=parse_size, default=parse_size(DEFAULT_IMAGE_SIZE),
                    help="the expected image_size as WxH (default %(default)s)")
    ap.add_argument("--source-prefix", default=DEFAULT_SOURCE_PREFIX,
                    help="every source-ref starts with this (default %(default)s)")
    args = ap.parse_args(argv)
    if args.images_color and not args.images_mono:
        ap.error("--images-color needs --images-mono (the cross-set identity compares the two)")
    classes = [c.strip() for c in args.classes.split(",") if c.strip()]

    failures: List[str] = []
    labeled: List[str] = []
    for path in args.labeled:
        try:
            labeled.extend(read_lines(path))
        except OSError as exc:
            print("error: cannot read {0}: {1}".format(path, exc), file=sys.stderr)
            return 1
    print("labeled: {0} lines from {1}".format(len(labeled), ", ".join(args.labeled)))
    if len(labeled) != args.expect_lines:
        failures.append("the labeled manifest(s) hold {0} lines, expected {1}".format(
            len(labeled), args.expect_lines))
    infos, shape_failures = shape_checks(labeled, args.image_size, args.source_prefix, classes)
    failures.extend(shape_failures)
    print("shape: {0} lines parsed, {1} failures; {2} lines with zero annotations".format(
        len(infos), len(shape_failures), sum(1 for i in infos if not i["labels"])))

    triage = None
    if args.triage:
        triage = assembly_triage.read_triage(args.triage)
        print("triage: Known_Good {0} / Fail_Set {1} / Occluded_Correct {2}".format(
            len(triage.known_good), len(triage.fail), len(triage.occluded_correct)))
        failures.extend("triage: " + p for p in triage.problems)
        _, without_row, without_image = triage.join(i["file"] for i in infos)
        failures.extend("image without a triage row: {0}".format(f) for f in sorted(without_row))
        failures.extend("triage row without an image: {0} [confidence={1}, notes={2!r}]".format(
            f, triage.rows[f].confidence, triage.rows[f].notes) for f in sorted(without_image))
        count_failures = count_checks(infos, triage)
        print("counts: {0} failures".format(len(count_failures)))
        failures.extend(count_failures)
    else:
        print("counts: not checked (no --triage)")

    if args.derived:
        try:
            derived = read_lines(args.derived)
        except OSError as exc:
            print("error: cannot read {0}: {1}".format(args.derived, exc), file=sys.stderr)
            return 1
        derived_failures = derived_checks(labeled, derived)
        print("derived: {0} lines, {1} failures".format(len(derived), len(derived_failures)))
        failures.extend(derived_failures)

    if args.images_mono:
        mono, n_mono, missing = replay_split(labeled, args.images_mono)
        failures.extend(missing)
        failures.extend(report_split("mono", mono, n_mono, triage))
        if args.images_color:
            color, n_color, missing = replay_split(labeled, args.images_color)
            failures.extend(missing)
            failures.extend(report_split("color", color, n_color, triage))
            differ = sorted(f for f in set(mono) | set(color) if mono.get(f) != color.get(f))
            print("cross-set split identity: {0}".format("identical" if not differ else
                                                         "{0} files differ".format(len(differ))))
            failures.extend("split differs across the image sets: {0} (mono {1}, color {2})".format(
                f, mono.get(f), color.get(f)) for f in differ)

    if failures:
        print("FAILED ({0}):".format(len(failures)))
        for failure in failures:
            print("  " + failure)
        return 1
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
