#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Reference capture and ``min_iou`` calibration for the Layout Check node
(car-assembly-check task 6.1; design §9, DD10, DD18, DD22, DD23;
Requirements 4.3, 4.4, 4.6, 10.9, 1.7).

**Capture** -- one Expected_Region per label from one correctly assembled
image, written as the node's ``expected_regions`` JSON:

    python3 datasets/layout_reference.py \\
        (--manifest m.manifest --source-ref s3://.../x.jpg \\
         | --overlay overlay.json (--metadata metadata.json | --size WxH)) \\
        --out expected_regions.json [--max-vertices 64] [--min-iou 0.98] \\
        [--triage placement-triage.csv]

The masks come from the manifest line's ``rle`` annotations (decoded with
``dda_manifest.rle_decode``) or from the saved body of the device's
``GET /workflows/executions/{id}/overlay`` (``instances[].mask_rle``), whose
frame size comes from the saved ``GET .../metadata`` body or ``--size``. With
``--triage`` a manifest image whose row is not Known_Good is refused (a
reference comes only from a triage-confirmed correct image). Per label: the
largest external contour, simplified with ``cv2.approxPolyDP`` at the
largest epsilon (searched geometrically, x1.5 from 0.5 px) that keeps at
most ``--max-vertices`` vertices and a raster IoU (``cv2.fillPoly`` vs the
mask) of at least ``--min-iou``; the vertices are normalized by the image
size at their pixel centres. Needs OpenCV (``opencv-python-headless``).

**Calibrate** -- the Position_Score distribution over the Known_Good_Set
and the recommended ``min_iou``:

    python3 datasets/layout_reference.py --calibrate m.manifest \\
        --regions expected_regions.json --triage placement-triage.csv \\
        [--test-files names.txt] [--min-confidence 0.5] [--min-iou v]
    python3 datasets/layout_reference.py --calibrate --results gray.json \\
        --local-server-version 1.0.7 --regions ... --triage ... [...]

``--results`` reads a Result_Export of ``samples/car-assembly-check/
batch_run.py``; its header must name ``--local-server-version`` before any
record is read (DD22). The table: per label min / p5 / median / max over
the Known_Good rows, the ``--test-files`` minimum, the swapped-fender and
vertically-flipped-roof margins, and ``recommended_min_iou = min(0.70,
floor05(min - 0.10))`` clipped to >= 0.30 (a WARNING below a 0.50
minimum). Fail_Set rows must fail with their triage-named pieces flagged or
missing; Occluded_Correct rows are printed without expectation. The
``--results`` form re-derives every verdict with ``layout.check_layout`` and
reports the outline margins (DD23). Calibration needs no OpenCV.

Exit codes: 0 ok, 1 a data problem (every listing named), 2 usage or
OpenCV missing for a capture. Local files only; no AWS SDK.
"""
import argparse
import json
import math
import re
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
from workflow_core.analytics import layout  # noqa: E402

VERSION_RE = re.compile(r"^\d+\.\d+\.\d+$")
IDENTITY_FIELDS = ("local_server_version", "local_server_version_end", "local_server_changed")
RECOMMENDED_CEILING = 0.70
RECOMMENDED_FLOOR = 0.30
RECOMMENDED_MARGIN = 0.10
WARN_BELOW = 0.50
NEAR_OUTLINE_BAND = (0.05, 0.50)
FENDERS = ("front_fender", "rear_fender")
ROOF = "roof"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _load_cv2():
    """``(cv2, numpy)``, or None after printing the install hint."""
    try:
        import cv2
        import numpy as np
        return cv2, np
    except ImportError:
        print("error: the reference capture needs OpenCV and numpy. Install them:\n"
              "    pip install opencv-python-headless numpy", file=sys.stderr)
        return None


def _read_json(path: str) -> Tuple[Any, Optional[str]]:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8")), None
    except (OSError, ValueError) as exc:
        return None, "{0}: {1}".format(path, exc)


def _manifest_lines(path: str) -> List[str]:
    return [line for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]


def _instance_entry(entry: dict) -> Optional[dict]:
    """A manifest line's ``(width, height, [(class, rle)])`` or None."""
    for key, value in entry.items():
        meta = entry.get(key + "-metadata")
        if isinstance(meta, dict) and meta.get("type") == "groundtruth/instance-segmentation":
            break
    else:
        key = "instance-segmentation"
    if not isinstance(entry.get(key), dict):
        return None
    parsed = dda_manifest._parse_instance_segmentation({
        "source-ref": entry.get("source-ref"),
        "instance-segmentation": entry[key],
        "instance-segmentation-metadata": entry.get(key + "-metadata") or {},
    })["annotation"]
    return {"width": parsed["image_size"]["width"], "height": parsed["image_size"]["height"],
            "instances": [(i["class"], i["rle"]) for i in parsed["instances"]]}


def _fmt(value: Optional[float]) -> str:
    return "-" if value is None else "{0:.4f}".format(value)


def floor05(value: float) -> float:
    return math.floor(value * 20.0 + 1e-9) / 20.0


def recommend(minimum: float) -> float:
    value = min(RECOMMENDED_CEILING, floor05(minimum - RECOMMENDED_MARGIN))
    return round(max(RECOMMENDED_FLOOR, value), 2)


def percentile(values: Sequence[float], fraction: float) -> float:
    """Nearest-rank percentile."""
    ordered = sorted(values)
    rank = max(1, int(math.ceil(fraction * len(ordered))))
    return ordered[rank - 1]


def flipped_vertically(polygon: Sequence[Sequence[float]]) -> List[List[float]]:
    """The region upside down (y -> 1 - y): an upside-down roof. A
    horizontal mirror of the half-disc is itself and is not used."""
    return [[float(x), 1.0 - float(y)] for x, y in polygon]


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def capture_masks_from_manifest(path: str, source_ref: str):
    """``(width, height, {label: [rle]}, filename, problem)``."""
    for line in _manifest_lines(path):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if entry.get("source-ref") != source_ref:
            continue
        try:
            info = _instance_entry(entry)
        except (KeyError, TypeError, ValueError) as exc:
            return None, None, None, None, "{0}: {1}".format(source_ref, exc)
        if info is None:
            return None, None, None, None, "{0}: not an instance-segmentation line".format(source_ref)
        masks: Dict[str, List[str]] = {}
        for cls, rle in info["instances"]:
            masks.setdefault(cls, []).append(rle)
        return info["width"], info["height"], masks, source_ref.rsplit("/", 1)[-1], None
    return None, None, None, None, "{0}: no line with this source-ref in {1}".format(source_ref, path)


def frame_from_metadata(metadata: Any) -> Optional[Tuple[int, int]]:
    """``layout.<nodeId>.frame`` of the first layout node, else the
    top-level ``frame``."""
    if not isinstance(metadata, dict):
        return None
    candidates = []
    sections = metadata.get("layout")
    if isinstance(sections, dict):
        for section in sections.values():
            if isinstance(section, dict):
                candidates.append(section.get("frame"))
                break
    candidates.append(metadata.get("frame"))
    for frame in candidates:
        if isinstance(frame, dict):
            width, height = frame.get("width"), frame.get("height")
            if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
                return width, height
    return None


def capture_masks_from_overlay(overlay: Any, size: Tuple[int, int]):
    """``({label: [rle]}, problems)`` from a saved overlay body."""
    problems = []
    if not isinstance(overlay, dict):
        return None, ["the overlay body is not a JSON object"]
    if overlay.get("detectionsTruncated") is True:
        problems.append("detectionsTruncated is true: a mask is incomplete")
    instances = overlay.get("instances")
    if not isinstance(instances, list):
        return None, problems + ["the overlay carries no instances list"]
    masks: Dict[str, List[str]] = {}
    for index, entry in enumerate(instances):
        if not isinstance(entry, dict):
            problems.append("instance {0}: not an object".format(index))
            continue
        label = str(entry.get("class_label"))
        if entry.get("mask_truncated"):
            problems.append("instance {0} ({1}): mask_truncated".format(index, label))
            continue
        if not isinstance(entry.get("mask_rle"), str):
            problems.append("instance {0} ({1}): no mask_rle".format(index, label))
            continue
        masks.setdefault(label, []).append(entry["mask_rle"])
    return masks, problems


def fit_polygon(cv2, np, mask, max_vertices: int, min_iou: float):
    """``(points, iou)`` of the largest epsilon meeting both limits, or
    ``(None, best_iou_seen)``."""
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    if not contours:
        return None, None
    contour = max(contours, key=cv2.contourArea)
    mask_on = mask > 0
    best = None
    best_seen = None
    epsilon = 0.5
    limit = float(max(mask.shape))
    while epsilon <= limit:
        approx = cv2.approxPolyDP(contour, epsilon, True)
        if len(approx) < 3:
            break
        if len(approx) <= max_vertices:
            canvas = np.zeros(mask.shape, dtype=np.uint8)
            cv2.fillPoly(canvas, [approx], 1)
            filled = canvas > 0
            union = np.logical_or(filled, mask_on).sum()
            iou = float(np.logical_and(filled, mask_on).sum()) / float(union) if union else 0.0
            best_seen = iou if best_seen is None else max(best_seen, iou)
            if iou >= min_iou:
                best = (approx.reshape(-1, 2), iou)
        epsilon *= 1.5
    if best is None:
        return None, best_seen
    return best


def run_capture(args) -> int:
    loaded = _load_cv2()
    if loaded is None:
        return 2
    cv2, np = loaded
    if args.manifest:
        width, height, masks, filename, problem = capture_masks_from_manifest(
            args.manifest, args.source_ref)
        if problem:
            print("error: " + problem)
            return 1
        if args.triage:
            triage = assembly_triage.read_triage(args.triage)
            if triage.problems:
                print("error: the Triage_File has problems:")
                for item in triage.problems:
                    print("  " + item)
                return 1
            row = triage.rows.get(filename)
            if row is None:
                print("error: {0} has no Triage_File row; a reference comes only from a "
                      "Known_Good image".format(filename))
                return 1
            if filename not in triage.known_good:
                print("error: {0} is not Known_Good ({1}); a reference comes only from a "
                      "triage-confirmed correct image without an occluder:\n  {2}".format(
                          filename, triage.set_of(filename), row.describe()))
                return 1
    else:
        if args.size:
            width, height = args.size
        else:
            metadata, problem = _read_json(args.metadata)
            if problem:
                print("error: " + problem)
                return 1
            frame = frame_from_metadata(metadata)
            if frame is None:
                print("error: {0} carries no frame size (layout.<nodeId>.frame or frame); "
                      "pass --size WxH".format(args.metadata))
                return 1
            width, height = frame
        overlay, problem = _read_json(args.overlay)
        if problem:
            print("error: " + problem)
            return 1
        masks, problems = capture_masks_from_overlay(overlay, (width, height))
        if problems:
            print("error: the overlay cannot give a reference:")
            for item in problems:
                print("  " + item)
            return 1

    failures = []
    regions: Dict[str, List[List[float]]] = {}
    rows = []
    for label in sorted(masks):
        rles = masks[label]
        if len(rles) > 1:
            failures.append("{0}: {1} instances (exactly one per label)".format(label, len(rles)))
            continue
        try:
            flat = dda_manifest.rle_decode(rles[0], width, height)
        except ValueError as exc:
            failures.append("{0}: {1}".format(label, exc))
            continue
        mask = np.frombuffer(bytes(flat), dtype=np.uint8).reshape(height, width).copy()
        points, iou = fit_polygon(cv2, np, mask, args.max_vertices, args.min_iou)
        if points is None:
            failures.append("{0}: IoU {1} unreachable with <= {2} vertices (best {3})".format(
                label, args.min_iou, args.max_vertices, _fmt(iou)))
            continue
        regions[label] = [[round(min(1.0, (float(x) + 0.5) / width), 6),
                           round(min(1.0, (float(y) + 0.5) / height), 6)] for x, y in points]
        rows.append((label, len(points), iou))
    for label in args.labels or []:
        if label not in masks:
            failures.append("{0}: missing (no instance of this label)".format(label))
    if not masks:
        failures.append("no instance in the source")
    if not failures:
        _, parse_problems = layout.parse_expected_regions(json.dumps(regions))
        failures.extend("expected_regions: " + p for p in parse_problems)
    if failures:
        print("error: nothing written:")
        for item in failures:
            print("  " + item)
        return 1
    print("label | vertices | iou")
    for label, vertices, iou in rows:
        print("{0} | {1} | {2:.4f}".format(label, vertices, iou))
    text = json.dumps(regions)
    Path(args.out).write_text(text + "\n", encoding="utf-8")
    print(text)
    print("wrote {0}".format(args.out))
    return 0


# ---------------------------------------------------------------------------
# Calibrate
# ---------------------------------------------------------------------------


def identity_gate(path: str, export: Any, version: str) -> List[str]:
    """DD22: the export's header names the build under test."""
    if not isinstance(export, dict):
        return ["{0}: not a Result_Export object".format(path)]
    absent = [name for name in IDENTITY_FIELDS if name not in export]
    if absent:
        return ["{0}: the header field(s) {1} are absent (an export from an older "
                "batch_run.py?); expected local_server_version {2}".format(
                    path, ", ".join(absent), version)]
    problems = []
    for name in ("local_server_version", "local_server_version_end"):
        if export[name] != version:
            problems.append("{0}: {1} is {2!r}, --local-server-version is {3!r}".format(
                path, name, export[name], version))
    if export["local_server_changed"] is not False:
        problems.append("{0}: local_server_changed is {1!r} (a revision landed mid-batch: "
                        "local_server_version {2!r}, local_server_version_end {3!r})".format(
                            path, export["local_server_changed"], export["local_server_version"],
                            export["local_server_version_end"]))
    return problems


def outline_zero_refusal(records: Sequence[dict], triage, path: str) -> List[str]:
    """DD23: an export echoing ``min_outline_overlap = 0`` while stray-piece
    rows are Known_Good cannot be calibrated."""
    stray = sorted(f for f in triage.known_good if triage.rows[f].unlabeled_stray > 0)
    if not stray:
        return []
    zero = [r.get("file") for r in records if isinstance(r, dict)
            and isinstance(r.get("layout"), dict)
            and r["layout"].get("min_outline_overlap") == 0]
    if not zero:
        return []
    return ["{0}: {1} record(s) echo min_outline_overlap = 0 (e.g. {2}) while the "
            "Known_Good_Set holds rows with unlabeled_stray > 0 ({3}); their pass "
            "expectation rests on the outline rule".format(path, len(zero), zero[0], ", ".join(stray))]


def _echoed_overlap(section: Any) -> float:
    value = section.get("min_outline_overlap") if isinstance(section, dict) else None
    if isinstance(value, (int, float)) and not isinstance(value, bool) \
            and 0.0 <= value <= layout.MAX_MIN_OUTLINE_OVERLAP:
        return float(value)
    return layout.DEFAULT_MIN_OUTLINE_OVERLAP


def _confidence(entry: dict) -> float:
    value = entry.get("confidence")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0


def entries_with_ids(instances: Sequence[dict], section: Any) -> List[dict]:
    """The raw instances with the Detection_IDs of the exported section
    (joined as on the device); an unjoined entry gets a synthetic id so the
    re-derivation does not warn about it."""
    built = []
    if isinstance(section, dict):
        detections = []
        for piece in (section.get("pieces") or {}).values():
            if isinstance(piece, dict):
                detections.extend(piece.get("detections") or [])
        detections.extend(section.get("ignored") or [])
        for detection in detections:
            box = detection.get("bounding_box") if isinstance(detection, dict) else None
            if isinstance(box, list) and len(box) == 4:
                built.append({"id": detection.get("id"), "label": detection.get("class_label"),
                              "confidence": detection.get("confidence"), "x_min": box[0],
                              "y_min": box[1], "x_max": box[2], "y_max": box[3]})
    joined = layout.attach_detection_ids(list(instances), built)
    for index, entry in enumerate(joined):
        if entry.get("id") is None:
            entry["id"] = "unjoined-{0}".format(index)
    return joined


class Image:
    """One image's instances for calibration, in one form."""

    def __init__(self, filename, width, height, entries, overlap_threshold, section=None):
        self.filename = filename
        self.width = width
        self.height = height
        self.entries = entries
        self.overlap_threshold = overlap_threshold
        self.section = section


def classify_instances(image: Image, regions: Dict[str, list], min_confidence: float):
    """Per label ``(counted, ignored)`` qualifying instances, each with its
    Outline_Overlap, at the image's echoed ``min_outline_overlap``."""
    outline = layout.outline_intervals(regions, image.width, image.height)
    result = {label: ([], []) for label in regions}
    for entry in image.entries:
        label = str(entry.get("class_label"))
        if label not in result or _confidence(entry) < min_confidence:
            continue
        overlap = None
        if not entry.get("mask_truncated"):
            mask, _ = layout.rle_intervals(entry.get("mask_rle"), image.width, image.height)
            if mask:
                overlap = round(layout.outline_overlap(mask, outline), 4)
        item = dict(entry, outline_overlap=overlap)
        if overlap is not None and overlap < image.overlap_threshold:
            result[label][1].append(item)
        else:
            result[label][0].append(item)
    return result


def _score(entry: dict, image: Image, polygon) -> Optional[float]:
    value, _ = layout.position_score(entry.get("mask_rle"), image.width, image.height, polygon)
    return value


def load_manifest_images(path: str) -> Tuple[List[Image], List[str]]:
    images, problems = [], []
    for number, line in enumerate(_manifest_lines(path), 1):
        try:
            entry = json.loads(line)
            info = _instance_entry(entry)
        except (KeyError, TypeError, ValueError) as exc:
            problems.append("line {0}: {1}".format(number, exc))
            continue
        if info is None:
            problems.append("line {0}: not an instance-segmentation line".format(number))
            continue
        filename = str(entry.get("source-ref")).rsplit("/", 1)[-1]
        entries = [{"id": "label-{0}".format(i), "class_label": cls, "confidence": 1.0,
                    "bounding_box": [0.0, 0.0, 0.0, 0.0], "mask_rle": rle}
                   for i, (cls, rle) in enumerate(info["instances"])]
        images.append(Image(filename, info["width"], info["height"], entries,
                            layout.DEFAULT_MIN_OUTLINE_OVERLAP))
    return images, problems


def load_result_images(records: Sequence[dict]) -> Tuple[List[Image], Dict[str, List[str]]]:
    """The comparable records and the excluded ones by reason."""
    images = []
    excluded: Dict[str, List[str]] = {"no layout.frame": [], "instances = null": [],
                                      "detections_truncated = true": []}
    for record in records:
        if not isinstance(record, dict):
            continue
        filename = Path(str(record.get("file"))).name
        section = record.get("layout")
        frame = section.get("frame") if isinstance(section, dict) else None
        if record.get("detections_truncated") is True:
            excluded["detections_truncated = true"].append(filename)
            continue
        if not isinstance(record.get("instances"), list):
            excluded["instances = null"].append(filename)
            continue
        if not (isinstance(frame, dict) and isinstance(frame.get("width"), int)
                and isinstance(frame.get("height"), int)):
            excluded["no layout.frame"].append(filename)
            continue
        entries = entries_with_ids([e for e in record["instances"] if isinstance(e, dict)], section)
        images.append(Image(filename, frame["width"], frame["height"], entries,
                            _echoed_overlap(section), section))
    return images, excluded


def statuses(result: Dict[str, Any]) -> Dict[str, str]:
    return {label: piece.get("status") for label, piece in (result.get("pieces") or {}).items()}


def run_calibrate(args) -> int:
    exit_code = 0
    source = "predicted" if args.results else "labels"
    regions, problems = layout.parse_expected_regions(
        Path(args.regions).read_text(encoding="utf-8") if Path(args.regions).is_file() else None)
    if regions is None:
        print("error: --regions {0} is not usable: {1}".format(args.regions, "; ".join(problems)))
        return 1
    triage = assembly_triage.read_triage(args.triage)
    test_files = assembly_triage.load_test_files(args.test_files) or set()

    records: List[dict] = []
    if args.results:
        export, problem = _read_json(args.results)
        if problem:
            print("error: " + problem)
            return 1
        gate = identity_gate(args.results, export, args.local_server_version)
        if gate:
            print("error: the identity gate (DD22) refuses the export; nothing computed:")
            for item in gate:
                print("  " + item)
            return 1
        records = [r for r in export.get("records") or [] if isinstance(r, dict)]
        refusal = outline_zero_refusal(records, triage, args.results)
        if refusal:
            print("error: " + refusal[0])
            return 1
        images, excluded = load_result_images(records)
        all_files = [Path(str(r.get("file"))).name for r in records]
    else:
        images, problems = load_manifest_images(args.calibrate)
        excluded = {"unreadable line": problems}
        all_files = [image.filename for image in images]

    print("source: {0}".format(source))
    if triage.problems:
        exit_code = 1
        print("triage problems (DD18):")
        for item in triage.problems:
            print("  " + item)
    _, without_row, without_image = triage.join(all_files)
    if without_row or without_image:
        exit_code = 1
        for name in sorted(without_row):
            print("image without a triage row: {0}".format(name))
        for name in sorted(without_image):
            print("triage row without an image: {0}".format(name))
    for reason, names in excluded.items():
        if names:
            exit_code = 1
            print("excluded ({0}): {1}".format(reason, ", ".join(sorted(names))))

    min_confidence = args.min_confidence
    distribution: Dict[str, List[float]] = {label: [] for label in regions}
    test_distribution: Dict[str, List[float]] = {label: [] for label in regions}
    swapped: Dict[str, List[float]] = {label: [] for label in regions}
    flipped: List[float] = []
    would_be_missing: List[str] = []
    duplicates: List[str] = []
    overlaps = {name: {"counted": [], "ignored": []} for name in
                (assembly_triage.SET_KNOWN_GOOD, assembly_triage.SET_FAIL,
                 assembly_triage.SET_OCCLUDED_CORRECT)}
    near_outline: List[str] = []
    classified = {}
    excluded_kg = set()

    for image in images:
        per_label = classify_instances(image, regions, min_confidence)
        classified[image.filename] = per_label
        set_name = triage.set_of(image.filename)
        for label, (counted, ignored) in per_label.items():
            if set_name in overlaps:
                overlaps[set_name]["counted"].extend(
                    i["outline_overlap"] for i in counted if i["outline_overlap"] is not None)
                overlaps[set_name]["ignored"].extend(i["outline_overlap"] for i in ignored)
            for item in counted + ignored:
                value = item["outline_overlap"]
                if value is not None and NEAR_OUTLINE_BAND[0] <= value < NEAR_OUTLINE_BAND[1]:
                    near_outline.append("{0} {1} (confidence {2:.3f}): outline_overlap {3:.4f}".format(
                        image.filename, label, _confidence(item), value))
        if set_name != assembly_triage.SET_KNOWN_GOOD:
            continue
        for label, (counted, _) in per_label.items():
            if not counted:
                would_be_missing.append("{0}: no qualifying {1}".format(image.filename, label))
                excluded_kg.add(image.filename)
            elif len(counted) > 1:
                best = max(counted, key=_confidence)
                duplicates.append("{0}: {1} x{2} (outline_overlap {3}); best scores {4}".format(
                    image.filename, label, len(counted),
                    ", ".join(_fmt(i["outline_overlap"]) for i in counted),
                    _fmt(_score(best, image, regions[label]))))
                excluded_kg.add(image.filename)
        if image.filename in excluded_kg:
            continue
        best = {label: max(counted, key=_confidence) for label, (counted, _) in per_label.items()}
        for label, entry in best.items():
            score = _score(entry, image, regions[label])
            if score is None:
                would_be_missing.append("{0}: {1} mask not decodable".format(image.filename, label))
                continue
            distribution[label].append(score)
            if image.filename in test_files:
                test_distribution[label].append(score)
        if all(f in regions for f in FENDERS):
            for label, other in (FENDERS, FENDERS[::-1]):
                value = _score(best[label], image, regions[other])
                if value is not None:
                    swapped[label].append(value)
        if ROOF in regions:
            value = _score(best[ROOF], image, flipped_vertically(regions[ROOF]))
            if value is not None:
                flipped.append(value)

    print("label | n | min | p5 | median | max | min(test) | swapped | flipped_roof | "
          "recommended_min_iou")
    recommendations = []
    minimum_all = None
    for label in sorted(regions):
        values = distribution[label]
        if not values:
            print("{0} | 0 | - | - | - | - | - | - | - | -".format(label))
            continue
        low = min(values)
        minimum_all = low if minimum_all is None else min(minimum_all, low)
        recommendation = recommend(low)
        recommendations.append(recommendation)
        print("{0} | {1} | {2} | {3} | {4} | {5} | {6} | {7} | {8} | {9:.2f}".format(
            label, len(values), _fmt(low), _fmt(percentile(values, 0.05)),
            _fmt(percentile(values, 0.5)), _fmt(max(values)),
            _fmt(min(test_distribution[label]) if test_distribution[label] else None),
            _fmt(max(swapped[label]) if swapped[label] else None),
            _fmt(max(flipped) if label == ROOF and flipped else None), recommendation))
    recommended = min(recommendations) if recommendations else None
    if recommended is None:
        print("recommended_min_iou: none (no Known_Good score)")
        exit_code = 1
    else:
        print("recommended_min_iou = {0:.2f} (source: {1})".format(recommended, source))
        if minimum_all < WARN_BELOW:
            print("WARNING: the Known_Good minimum {0:.4f} is below {1:.2f} -- the reference, "
                  "the labels or the model is suspect".format(minimum_all, WARN_BELOW))
    if would_be_missing:
        exit_code = 1
        print("would-be missing (Known_Good false-fails, excluded from the distribution):")
        for item in would_be_missing:
            print("  " + item)
    if duplicates:
        exit_code = 1
        print("duplicate -- not a calibration matter (excluded from the distribution and the "
              "recommendation):")
        for item in duplicates:
            print("  " + item)

    threshold = args.min_iou if args.min_iou is not None else (
        recommended if recommended is not None else layout.DEFAULT_MIN_IOU)
    print("check at min_iou = {0:.2f}".format(threshold))
    false_fails, fail_listing, fail_occluded, occluded_block, disagreements = [], [], [], [], []
    for image in images:
        set_name = triage.set_of(image.filename)
        result = layout.check_layout(image.entries, (image.width, image.height), regions,
                                     min_iou=threshold, min_confidence=min_confidence,
                                     min_outline_overlap=image.overlap_threshold)
        status = statuses(result)
        scores = ", ".join("{0} {1}".format(label, _fmt(piece.get("iou")))
                           for label, piece in sorted(result["pieces"].items()))
        if set_name == assembly_triage.SET_KNOWN_GOOD and image.filename not in excluded_kg \
                and not result["pass"]:
            false_fails.append("{0}: {1} ({2})".format(image.filename, status, scores))
        elif set_name == assembly_triage.SET_FAIL:
            expected = triage.expected_pieces(image.filename)
            not_flagged = sorted(p for p in expected if status.get(p) == layout.STATUS_OK)
            if result["pass"] or not_flagged:
                row = triage.rows[image.filename]
                text = "{0} | {1} | {2} | {3} | {4} ({5})".format(
                    image.filename, row.set, ";".join(sorted(expected)),
                    "pass" if result["pass"] else "fail", ";".join(not_flagged) or "-", scores)
                (fail_occluded if row.has_occluder else fail_listing).append(text)
        elif set_name == assembly_triage.SET_OCCLUDED_CORRECT:
            occluded_block.append("{0}: {1} ({2})".format(
                image.filename, "pass" if result["pass"] else "fail", scores))
        if image.section is not None:
            derived = layout.check_layout(
                image.entries, (image.width, image.height), regions,
                min_iou=args.min_iou if args.min_iou is not None
                else image.section.get("min_iou", layout.DEFAULT_MIN_IOU),
                min_confidence=image.section.get("min_confidence", min_confidence),
                min_outline_overlap=image.overlap_threshold)
            exported = {"pass": image.section.get("pass"), "pieces": statuses(image.section)}
            if exported != {"pass": derived["pass"], "pieces": statuses(derived)}:
                disagreements.append("{0}: exported pass={1} {2}, re-derived pass={3} {4}".format(
                    image.filename, exported["pass"], exported["pieces"], derived["pass"],
                    statuses(derived)))
    if false_fails:
        exit_code = 1
        print("Known_Good false-fails at min_iou {0:.2f}:".format(threshold))
        for item in false_fails:
            print("  " + item)
    print("Fail_Set true-fail check (file | set | expected pieces | verdict | pieces not flagged):")
    for item in fail_listing:
        print("  " + item)
    if not fail_listing:
        print("  (empty: every Fail_Set image fails with its pieces flagged or missing)")
    if fail_occluded:
        print("Fail_Set, occluded (file | set | expected pieces | verdict | pieces not flagged):")
        for item in fail_occluded:
            print("  " + item)
    if fail_listing or fail_occluded:
        exit_code = 1
        if args.results:
            print("  a triage-named piece reported ok is the class-from-position signal (O13)")
    print("Occluded_Correct (no expectation):")
    for item in occluded_block:
        print("  " + item)
    if args.results:
        print("re-derived verdicts: {0} disagreement(s)".format(len(disagreements)))
        for item in disagreements:
            print("  " + item)
        if disagreements:
            exit_code = 1
        print("outline report (DD23):")
        for set_name, values in overlaps.items():
            print("  {0}: min outline_overlap counted {1}, max ignored {2}".format(
                set_name, _fmt(min(values["counted"]) if values["counted"] else None),
                _fmt(max(values["ignored"]) if values["ignored"] else None)))
        print("  near the car outline [{0}, {1}):".format(*NEAR_OUTLINE_BAND))
        for item in near_outline:
            print("    " + item)
    return exit_code


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def _size(value: str) -> Tuple[int, int]:
    try:
        width, height = (int(part) for part in value.lower().split("x"))
    except ValueError:
        raise argparse.ArgumentTypeError("expected WxH, got {0!r}".format(value))
    if width < 1 or height < 1:
        raise argparse.ArgumentTypeError("expected a positive WxH, got {0!r}".format(value))
    return width, height


def _version(value: str) -> str:
    if not VERSION_RE.match(value):
        raise argparse.ArgumentTypeError("expected N.N.N, got {0!r}".format(value))
    return value


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(
        description="Layout Check reference capture (expected_regions) and min_iou calibration.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", help="capture: the labeled manifest")
    ap.add_argument("--source-ref", help="capture: the manifest line's source-ref")
    ap.add_argument("--overlay", help="capture: a saved GET .../overlay body")
    ap.add_argument("--metadata", help="capture: the saved GET .../metadata body (frame size)")
    ap.add_argument("--size", type=_size, help="capture: the frame size WxH")
    ap.add_argument("--out", help="capture: the expected_regions JSON to write")
    ap.add_argument("--max-vertices", type=int, default=layout.MAX_VERTICES)
    ap.add_argument("--labels", type=lambda v: [x for x in v.split(",") if x],
                    default=list(assembly_triage.PIECE_CLASSES),
                    help="capture: the labels that must be present (default %(default)s)")
    ap.add_argument("--min-iou", type=float, default=None,
                    help="capture: polygon IoU floor (default 0.98); calibrate: the deployed "
                         "min_iou for the checks (default: the recommendation)")
    ap.add_argument("--triage", help="placement-triage.csv (eleven columns)")
    ap.add_argument("--calibrate", nargs="?", const="", metavar="MANIFEST",
                    help="calibrate from a labeled manifest, or with --results")
    ap.add_argument("--results", help="calibrate: a Result_Export of batch_run.py")
    ap.add_argument("--local-server-version", type=_version,
                    help="required with --results: the build under test (N.N.N)")
    ap.add_argument("--regions", help="calibrate: the expected_regions JSON")
    ap.add_argument("--test-files", help="calibrate: the test split's file names (file or list)")
    ap.add_argument("--min-confidence", type=float, default=layout.DEFAULT_MIN_CONFIDENCE)
    return ap


def main(argv=None) -> int:
    ap = build_parser()
    args = ap.parse_args(argv)
    if args.calibrate is not None:
        if bool(args.calibrate) == bool(args.results):
            ap.error("--calibrate takes either a manifest or --results <Result_Export>")
        if args.results and not args.local_server_version:
            ap.error("--local-server-version is required with --results (DD22)")
        if not args.regions or not args.triage:
            ap.error("--calibrate needs --regions and --triage")
        return run_calibrate(args)
    if args.results:
        ap.error("--results belongs to --calibrate")
    if bool(args.manifest) == bool(args.overlay):
        ap.error("capture takes either --manifest with --source-ref or --overlay")
    if args.manifest and not args.source_ref:
        ap.error("--manifest needs --source-ref")
    if args.overlay and not (args.metadata or args.size):
        print("error: --overlay needs --metadata or --size: the overlay body carries no "
              "frame size", file=sys.stderr)
        return 1
    if not args.out:
        ap.error("capture needs --out")
    if args.min_iou is None:
        args.min_iou = 0.98
    return run_capture(args)


if __name__ == "__main__":
    sys.exit(main())
