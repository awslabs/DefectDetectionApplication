#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
The grayscale / colour A/B comparison table (car-assembly-check task 15.1;
design §12 step 4, DD12, DD18, DD22, DD23; Requirements 11.3, 11.5, 11.7).

    python3 datasets/compare_variants.py \\
        --gray-meta gray/training_metadata.json --color-meta color/training_metadata.json \\
        --gray-results gray.json --color-results color.json \\
        --labeled output.manifest --regions expected_regions.json \\
        --triage placement-triage.csv --gray-min-iou 0.65 --color-min-iou 0.70 \\
        --local-server-version 1.0.7 --test-files test-names.txt --out table.md \\
        [--no-m2] [--no-m4] [--label job=car-assembly-instances-r2 ...]

**Identity gate first (DD22).** Both Result_Exports' headers must carry
``local_server_version`` and ``local_server_version_end`` equal to
``--local-server-version`` and ``local_server_changed`` false; otherwise
exit 1 naming the file and the values, nothing computed. An export echoing
``min_outline_overlap = 0`` while the Known_Good_Set holds stray-piece rows
is refused the same way (DD23).

Metrics (defaults M1-M4; ``--no-m2`` / ``--no-m4`` switch two off):

* **M1** -- ``test_map50``, ``test_map50_95``, ``test_mask_map50``,
  ``test_mask_map50_95`` from each ``training_metadata.json`` (``metrics``).
* **M2** -- per-class mask IoU of each record's best qualifying instance
  against the labeled RLE, on the record's ``layout.frame`` grid (the label
  resampled by nearest neighbour when the sizes differ: OpenCV
  ``INTER_NEAREST``, else Pillow ``NEAREST``), for the test files and for
  all files; a record without a frame or a qualifying instance is "not
  comparable" for that class.
* **M3** -- the device verdicts split by the Triage_File: the false-fail
  rate over the Known_Good records, the false-pass rate over the Fail_Set
  records, the per-piece agreement (occluded Fail_Set images in their own
  row), the Occluded_Correct verdicts without expectation, and every
  Fail_Set piece reported ``ok`` (the O13 signal).
* **M4** -- median and p95 of ``timings_ms.model``, ``.gray`` (grayscale
  only), ``.layout`` and ``.total``.

Every record's verdict is re-derived with ``layout.check_layout`` at its
variant's threshold and its echoed ``min_outline_overlap``; a disagreement
with the exported section marks the row and sets exit 1. The ranking
(false-fail, then false-pass, then M1 mask mAP50-95 with 0.02 a tie, then
M4 total median; a remaining tie keeps grayscale) is printed; the decision
is the owner's.

Exit codes: 0 table written, 1 a refusal or a marked problem that
invalidates the comparison (file-set mismatch, triage problem,
disagreement), 2 usage. Local files only; no AWS SDK.
"""
import argparse
import json
import math
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

_DATASETS = Path(__file__).resolve().parent
_LAYERS = _DATASETS.parent / "edge-cv-portal" / "backend" / "layers"
for _path in (str(_DATASETS), str(_LAYERS / "shared" / "python"),
              str(_LAYERS / "workflow_core" / "python")):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import assembly_triage  # noqa: E402
import dda_manifest  # noqa: E402
import layout_reference  # noqa: E402
from workflow_core.analytics import layout  # noqa: E402

M1_KEYS = ("test_map50", "test_map50_95", "test_mask_map50", "test_mask_map50_95")
TIMING_KEYS = ("model", "gray", "layout", "total")
MAP_TIE = 0.02
VARIANTS = ("grayscale", "colour")


def _fmt(value: Optional[float], digits: int = 4) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "n/a"
    return "{0:.{1}f}".format(value, digits)


def _rate(numerator: int, denominator: int) -> Optional[float]:
    return numerator / denominator if denominator else None


def _median(values: Sequence[float]) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[middle])
    return (ordered[middle - 1] + ordered[middle]) / 2.0


def _p95(values: Sequence[float]) -> Optional[float]:
    return float(layout_reference.percentile(values, 0.95)) if values else None


# ---------------------------------------------------------------------------
# M2: one pixel grid
# ---------------------------------------------------------------------------


def resample_nearest(mask, width: int, height: int):
    """A boolean ``(rows, columns)`` array resampled to ``(height, width)``."""
    import numpy as np
    if mask.shape == (height, width):
        return mask
    try:
        import cv2
        return cv2.resize(mask.astype(np.uint8), (width, height),
                          interpolation=cv2.INTER_NEAREST) > 0
    except ImportError:
        from PIL import Image
        image = Image.fromarray(mask.astype(np.uint8) * 255)
        return np.asarray(image.resize((width, height), Image.NEAREST)) > 0


def mask_intervals(mask) -> List[Tuple[int, int]]:
    """Column-major (Fortran-order) runs of a boolean array as intervals."""
    import numpy as np
    flat = mask.flatten(order="F").astype(np.int8)
    edges = np.diff(np.concatenate(([0], flat, [0])))
    starts = np.flatnonzero(edges == 1)
    ends = np.flatnonzero(edges == -1)
    return [(int(a), int(b)) for a, b in zip(starts, ends)]


def label_mask(rles: Sequence[str], width: int, height: int):
    import numpy as np
    union = np.zeros((height, width), dtype=bool)
    for rle in rles:
        flat = dda_manifest.rle_decode(rle, width, height)
        union |= np.frombuffer(bytes(flat), dtype=np.uint8).reshape(height, width) > 0
    return union


def m2_iou(label_rles: Sequence[str], label_size: Tuple[int, int], instance: dict,
           frame: Tuple[int, int]) -> Optional[float]:
    fw, fh = frame
    predicted, problem = layout.rle_intervals(instance.get("mask_rle"), fw, fh)
    if predicted is None:
        return None
    mask = resample_nearest(label_mask(label_rles, *label_size), fw, fh)
    return layout.interval_iou(mask_intervals(mask), predicted)


# ---------------------------------------------------------------------------
# Inputs
# ---------------------------------------------------------------------------


def load_labels(path: str) -> Dict[str, dict]:
    """``{filename: {size, masks: {class: [rle]}}}`` of the labeled manifest."""
    labels = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        entry = json.loads(line)
        info = layout_reference._instance_entry(entry)
        if info is None:
            continue
        masks: Dict[str, List[str]] = {}
        for cls, rle in info["instances"]:
            masks.setdefault(cls, []).append(rle)
        labels[str(entry.get("source-ref")).rsplit("/", 1)[-1]] = {
            "size": (info["width"], info["height"]), "masks": masks}
    return labels


def load_m1(path: str) -> Dict[str, Optional[float]]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    metrics = data.get("metrics") if isinstance(data.get("metrics"), dict) else data
    values = {}
    for key in M1_KEYS:
        value = metrics.get(key)
        values[key] = float(value) if isinstance(value, (int, float)) else None
    return values


def best_instance(record: dict, label: str, min_confidence: float) -> Optional[dict]:
    instances = record.get("instances")
    if not isinstance(instances, list):
        return None
    qualifying = [e for e in instances if isinstance(e, dict)
                  and str(e.get("class_label")) == label
                  and layout_reference._confidence(e) >= min_confidence]
    return max(qualifying, key=layout_reference._confidence) if qualifying else None


def record_frame(record: dict) -> Optional[Tuple[int, int]]:
    section = record.get("layout")
    frame = section.get("frame") if isinstance(section, dict) else None
    if isinstance(frame, dict) and isinstance(frame.get("width"), int) \
            and isinstance(frame.get("height"), int):
        return frame["width"], frame["height"]
    return None


# ---------------------------------------------------------------------------
# One variant
# ---------------------------------------------------------------------------


def evaluate(name: str, records: List[dict], threshold: float, regions: dict, triage,
             labels: Dict[str, dict], test_files: set, want_m2: bool) -> dict:
    marks: List[str] = []
    disagreements: List[str] = []
    m3 = {"kg": 0, "false_fail": 0, "fail": 0, "false_pass": 0, "agree": 0,
          "fail_occluded": 0, "agree_occluded": 0, "no_verdict": 0}
    o13: List[str] = []
    occluded_correct: List[str] = []
    timings: Dict[str, List[float]] = {key: [] for key in TIMING_KEYS}
    m2: Dict[str, Dict[str, List[float]]] = {label: {"test": [], "all": []} for label in regions}
    m2_not_comparable: List[str] = []

    for record in records:
        filename = Path(str(record.get("file"))).name
        section = record.get("layout")
        if filename not in labels or filename not in triage.rows:
            marks.append("{0}: no {1} match".format(
                filename, "manifest" if filename not in labels else "triage"))
        if record.get("instances") is None:
            marks.append("{0}: instances = null".format(filename))
        if record.get("detections_truncated") is True:
            marks.append("{0}: detections_truncated = true".format(filename))
        for key in TIMING_KEYS:
            if key == "gray" and name != "grayscale":
                continue
            value = (record.get("timings_ms") or {}).get(key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                timings[key].append(float(value))
            else:
                marks.append("{0}: timings_ms.{1} missing".format(filename, key))

        # M3 from the device verdict.
        exported_pass = section.get("pass") if isinstance(section, dict) else None
        exported = layout_reference.statuses(section) if isinstance(section, dict) else {}
        set_name = triage.set_of(filename)
        if exported_pass is None:
            m3["no_verdict"] += 1
            marks.append("{0}: no layout verdict".format(filename))
        if set_name == assembly_triage.SET_KNOWN_GOOD:
            m3["kg"] += 1
            if exported_pass is False:
                m3["false_fail"] += 1
        elif set_name == assembly_triage.SET_FAIL:
            occluded = triage.rows[filename].has_occluder
            m3["fail_occluded" if occluded else "fail"] += 1
            if exported_pass is True:
                m3["false_pass"] += 1
            expected = triage.expected_pieces(filename)
            flagged_or_missing = {label for label, status in exported.items()
                                  if status != layout.STATUS_OK}
            if expected <= flagged_or_missing:
                m3["agree_occluded" if occluded else "agree"] += 1
            for piece in sorted(expected):
                if exported.get(piece) == layout.STATUS_OK:
                    o13.append("{0}: {1} reported ok (O13 signal)".format(filename, piece))
        elif set_name == assembly_triage.SET_OCCLUDED_CORRECT:
            occluded_correct.append("{0}: {1}".format(filename, {True: "pass", False: "fail"}.get(
                exported_pass, "no verdict")))

        # The consistency check at this variant's threshold.
        frame = record_frame(record)
        if isinstance(section, dict) and frame is not None and isinstance(record.get("instances"), list):
            entries = layout_reference.entries_with_ids(
                [e for e in record["instances"] if isinstance(e, dict)], section)
            derived = layout.check_layout(
                entries, frame, regions, min_iou=threshold,
                min_confidence=section.get("min_confidence", layout.DEFAULT_MIN_CONFIDENCE),
                min_outline_overlap=layout_reference._echoed_overlap(section))
            if derived["pass"] != exported_pass or layout_reference.statuses(derived) != exported:
                disagreements.append("{0}: exported pass={1} {2}, re-derived at {3:.2f} pass={4} {5}".format(
                    filename, exported_pass, exported, threshold, derived["pass"],
                    layout_reference.statuses(derived)))

        # M2 on the record's grid.
        if want_m2:
            label_info = labels.get(filename)
            min_confidence = (section or {}).get("min_confidence", layout.DEFAULT_MIN_CONFIDENCE) \
                if isinstance(section, dict) else layout.DEFAULT_MIN_CONFIDENCE
            for label in regions:
                instance = best_instance(record, label, min_confidence)
                rles = (label_info or {}).get("masks", {}).get(label)
                if frame is None or instance is None or not rles:
                    m2_not_comparable.append("{0} {1}".format(filename, label))
                    continue
                value = m2_iou(rles, label_info["size"], instance, frame)
                if value is None:
                    m2_not_comparable.append("{0} {1}".format(filename, label))
                    continue
                m2[label]["all"].append(value)
                if filename in test_files:
                    m2[label]["test"].append(value)

    grid = sorted({"{0}x{1}".format(*f) for f in (record_frame(r) for r in records) if f})
    return {"name": name, "threshold": threshold, "marks": marks, "disagreements": disagreements,
            "m3": m3, "o13": o13, "occluded_correct": occluded_correct, "timings": timings,
            "m2": m2, "m2_not_comparable": m2_not_comparable, "grid": grid,
            "false_fail_rate": _rate(m3["false_fail"], m3["kg"]),
            "false_pass_rate": _rate(m3["false_pass"], m3["fail"] + m3["fail_occluded"])}


def rank(gray: dict, color: dict, m1: Dict[str, Dict[str, Optional[float]]],
         want_m4: bool) -> Tuple[List[str], str]:
    """The ranked order under the criterion of 0.5 and the deciding step."""
    def lower(a, b):
        if a is None or b is None or a == b:
            return None
        return "grayscale" if a < b else "colour"

    for metric in ("false_fail_rate", "false_pass_rate"):
        winner = lower(gray[metric], color[metric])
        if winner:
            return _order(winner), metric.replace("_", "-").replace("-rate", " rate")
    g_map, c_map = m1["grayscale"]["test_mask_map50_95"], m1["colour"]["test_mask_map50_95"]
    if g_map is not None and c_map is not None and abs(g_map - c_map) > MAP_TIE:
        return _order("grayscale" if g_map > c_map else "colour"), "M1 mask mAP50-95"
    if want_m4:
        winner = lower(_median(gray["timings"]["total"]), _median(color["timings"]["total"]))
        if winner:
            return _order(winner), "M4 total median"
    return _order("grayscale"), "tie (the production default, grayscale)"


def _order(winner: str) -> List[str]:
    return [winner, "colour" if winner == "grayscale" else "grayscale"]


def table(gray: dict, color: dict, m1, args, order: List[str], decided_by: str) -> str:
    lines = ["# Grayscale vs colour (car-assembly-check A/B)", ""]
    for item in args.label or []:
        lines.append("- {0}".format(item))
    lines.append("- local_server_version: {0}".format(args.local_server_version))
    lines.append("- thresholds: grayscale min_iou {0:.2f}, colour min_iou {1:.2f}".format(
        gray["threshold"], color["threshold"]))
    lines.append("")
    lines.append("| metric | grayscale | colour |")
    lines.append("|---|---|---|")
    for key in M1_KEYS:
        lines.append("| M1 {0} | {1} | {2} |".format(key, _fmt(m1["grayscale"][key]), _fmt(m1["colour"][key])))
    for variant in (gray, color):
        variant["m3_rows"] = {
            "false-fail (Known_Good)": "{0}/{1} = {2}".format(
                variant["m3"]["false_fail"], variant["m3"]["kg"], _fmt(variant["false_fail_rate"])),
            "false-pass (Fail_Set)": "{0}/{1} = {2}".format(
                variant["m3"]["false_pass"], variant["m3"]["fail"] + variant["m3"]["fail_occluded"],
                _fmt(variant["false_pass_rate"])),
            "per-piece agreement (Fail_Set, no occluder)": "{0}/{1}".format(
                variant["m3"]["agree"], variant["m3"]["fail"]),
            "per-piece agreement (Fail_Set, occluded)": "{0}/{1}".format(
                variant["m3"]["agree_occluded"], variant["m3"]["fail_occluded"]),
            "Occluded_Correct verdicts (no expectation)": "; ".join(variant["occluded_correct"]) or "-",
            "Fail_Set pieces reported ok (O13)": "; ".join(variant["o13"]) or "-",
        }
    for row in gray["m3_rows"]:
        lines.append("| M3 {0} | {1} | {2} |".format(row, gray["m3_rows"][row], color["m3_rows"][row]))
    if not args.no_m2:
        lines.append("| M2 grid | {0} | {1} |".format(", ".join(gray["grid"]) or "n/a",
                                                      ", ".join(color["grid"]) or "n/a"))
        for label in sorted(gray["m2"]):
            for scope in ("test", "all"):
                cells = []
                for variant in (gray, color):
                    values = variant["m2"][label][scope]
                    cells.append("mean {0} / min {1} (n={2})".format(
                        _fmt(sum(values) / len(values) if values else None),
                        _fmt(min(values) if values else None), len(values)))
                lines.append("| M2 {0} ({1}) | {2} | {3} |".format(label, scope, *cells))
        lines.append("| M2 not comparable | {0} | {1} |".format(
            "; ".join(gray["m2_not_comparable"]) or "-", "; ".join(color["m2_not_comparable"]) or "-"))
    if not args.no_m4:
        for key in TIMING_KEYS:
            cells = []
            for variant in (gray, color):
                values = variant["timings"][key]
                if key == "gray" and variant is color:
                    cells.append("-")
                else:
                    cells.append("median {0} / p95 {1}".format(_fmt(_median(values), 1),
                                                               _fmt(_p95(values), 1)))
            lines.append("| M4 timings_ms.{0} | {1} | {2} |".format(key, *cells))
    lines.append("| re-derivation disagreements | {0} | {1} |".format(
        "; ".join(gray["disagreements"]) or "0", "; ".join(color["disagreements"]) or "0"))
    lines.append("| marks | {0} | {1} |".format("; ".join(gray["marks"]) or "-",
                                                 "; ".join(color["marks"]) or "-"))
    lines.append("")
    lines.append("Ranked order: 1. {0}, 2. {1} (decided by: {2}). The decision is the owner's (22.4).".format(
        order[0], order[1], decided_by))
    return "\n".join(lines) + "\n"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="The car-assembly-check A/B comparison table.")
    for flag in ("--gray-meta", "--color-meta", "--gray-results", "--color-results",
                 "--labeled", "--regions", "--triage", "--out"):
        ap.add_argument(flag, required=True)
    ap.add_argument("--gray-min-iou", type=float, required=True)
    ap.add_argument("--color-min-iou", type=float, required=True)
    ap.add_argument("--local-server-version", type=layout_reference._version, required=True,
                    help="the build under test (N.N.N, DD22)")
    ap.add_argument("--test-files", help="the test split's file names (file or comma list)")
    ap.add_argument("--no-m2", action="store_true", help="switch M2 off")
    ap.add_argument("--no-m4", action="store_true", help="switch M4 off")
    ap.add_argument("--label", action="append", metavar="K=V", help="a k=v line for the table")
    args = ap.parse_args(argv)

    exports = {}
    gate: List[str] = []
    for variant, path in (("grayscale", args.gray_results), ("colour", args.color_results)):
        value, problem = layout_reference._read_json(path)
        if problem:
            gate.append(problem)
            continue
        exports[variant] = value
        gate.extend(layout_reference.identity_gate(path, value, args.local_server_version))
    if not gate and exports["grayscale"]["local_server_version"] != exports["colour"]["local_server_version"]:
        gate.append("{0} and {1} name different builds".format(args.gray_results, args.color_results))
    if gate:
        print("error: the identity gate (DD22) refuses; nothing computed:")
        for item in gate:
            print("  " + item)
        return 1

    triage = assembly_triage.read_triage(args.triage)
    refusals = []
    for variant, path in (("grayscale", args.gray_results), ("colour", args.color_results)):
        records = [r for r in exports[variant].get("records") or [] if isinstance(r, dict)]
        refusals.extend(layout_reference.outline_zero_refusal(records, triage, path))
    if refusals:
        print("error: " + "\n  ".join(refusals))
        return 1

    regions, problems = layout.parse_expected_regions(Path(args.regions).read_text(encoding="utf-8"))
    if regions is None:
        print("error: --regions is not usable: " + "; ".join(problems))
        return 1
    labels = load_labels(args.labeled)
    test_files = assembly_triage.load_test_files(args.test_files) or set()
    m1 = {"grayscale": load_m1(args.gray_meta), "colour": load_m1(args.color_meta)}

    exit_code = 0
    if triage.problems:
        exit_code = 1
        print("triage problems (DD18):")
        for item in triage.problems:
            print("  " + item)
    files = {}
    for variant in VARIANTS:
        files[variant] = sorted(Path(str(r.get("file"))).name
                                for r in exports[variant].get("records") or [] if isinstance(r, dict))
    if set(files["grayscale"]) != set(files["colour"]):
        exit_code = 1
        print("file-set mismatch: only grayscale {0}; only colour {1}".format(
            sorted(set(files["grayscale"]) - set(files["colour"])),
            sorted(set(files["colour"]) - set(files["grayscale"]))))

    results = {}
    for variant, threshold in (("grayscale", args.gray_min_iou), ("colour", args.color_min_iou)):
        records = [r for r in exports[variant].get("records") or [] if isinstance(r, dict)]
        results[variant] = evaluate(variant, records, threshold, regions, triage, labels,
                                    test_files, not args.no_m2)
        if results[variant]["disagreements"]:
            exit_code = 1
    order, decided_by = rank(results["grayscale"], results["colour"], m1, not args.no_m4)
    text = table(results["grayscale"], results["colour"], m1, args, order, decided_by)
    Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    print("wrote {0}".format(args.out))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
