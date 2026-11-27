# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for `datasets/layout_reference.py` (car-assembly-check task 6.2,
repository part; design §9, DD10, DD18, DD22, DD23).

A synthetic 120 x 80 frame in the Training_Orientation: `rear_fender` in
the LEFT pocket, `front_fender` in the RIGHT pocket (rectangles), `roof` a
half-disc on top, flat side down. Masks are Canonical_RLE strings from
`dda_manifest.rle_encode`; the triage CSV uses the real eleven-column
vocabulary.

The capture cases need OpenCV and skip with that reason when `cv2` is
absent (the Portal venv); the calibration cases use only the shared
`layout` module and run everywhere.
"""
import json
import math
import os
import re
import sys

import pytest

import dda_manifest
from workflow_core.analytics import layout

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATASETS = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "datasets"))
if _DATASETS not in sys.path:
    sys.path.append(_DATASETS)

import assembly_triage  # noqa: E402
import layout_reference as lr  # noqa: E402

try:
    import cv2  # noqa: F401
    HAVE_CV2 = True
except ImportError:
    HAVE_CV2 = False
needs_cv2 = pytest.mark.skipif(not HAVE_CV2, reason="OpenCV (cv2) is not installed: the "
                               "reference capture needs it (run in the python3 lane)")

W, H = 120, 80
VERSION = "1.0.7"
PREFIX = "s3://bucket/assembly-photos/"
CLASSES = ["front_fender", "rear_fender", "roof"]
HEADER = ("filename,set,source_path,verdict,misplaced_pieces,missing_pieces,"
          "occluder,confidence,notes,piece_counts,unlabeled_stray")

REAR_RECT = (10, 40, 50, 70)
FRONT_RECT = (60, 40, 110, 70)
ROOF_DISC = (60.0, 38.0, 25.0)


# ---------------------------------------------------------------------------
# Synthetic geometry
# ---------------------------------------------------------------------------


def rect_pixels(rect, dx=0, dy=0):
    x0, y0, x1, y1 = rect
    return {(x + dx, y + dy) for x in range(x0, x1) for y in range(y0, y1)
            if 0 <= x + dx < W and 0 <= y + dy < H}


def disc_pixels(dx=0, dy=0, flat_up=False):
    cx, cy, r = ROOF_DISC
    pixels = set()
    for x in range(W):
        for y in range(H):
            px, py = x + 0.5 - dx, y + 0.5 - dy
            inside = (px - cx) ** 2 + (py - cy) ** 2 <= r * r
            half = py >= cy if flat_up else py <= cy
            if inside and half:
                pixels.add((x, y))
    return pixels


def rle(pixels, width=W, height=H):
    mask = [0] * (width * height)
    for x, y in pixels:
        mask[y * width + x] = 1
    return dda_manifest.rle_encode(mask, width, height)


def box(pixels):
    xs = [p[0] for p in pixels]
    ys = [p[1] for p in pixels]
    return [float(min(xs)), float(min(ys)), float(max(xs) + 1), float(max(ys) + 1)]


def norm_rect(rect):
    x0, y0, x1, y1 = rect
    return [[x0 / W, y0 / H], [x1 / W, y0 / H], [x1 / W, y1 / H], [x0 / W, y1 / H]]


def roof_polygon():
    cx, cy, r = ROOF_DISC
    points = [[(cx + r * math.cos(math.pi * k / 32)) / W, (cy - r * math.sin(math.pi * k / 32)) / H]
              for k in range(33)]
    return points


REGIONS = {"rear_fender": norm_rect(REAR_RECT), "front_fender": norm_rect(FRONT_RECT),
           "roof": roof_polygon()}


def pieces(dx=0, roof=True, swapped=False, extra=None):
    """[(class, pixels)] for one image."""
    rear, front = rect_pixels(REAR_RECT, dx), rect_pixels(FRONT_RECT, dx)
    if swapped:
        rear, front = rect_pixels(FRONT_RECT, dx), rect_pixels(REAR_RECT, dx)
    items = [("front_fender", front), ("rear_fender", rear)]
    if roof:
        items.append(("roof", disc_pixels(dx)))
    return items + list(extra or [])


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------


def manifest_line(name, items):
    annotations = []
    for cls, pixels in items:
        x0, y0, x1, y1 = box(pixels)
        annotations.append({"class_id": CLASSES.index(cls), "left": int(x0), "top": int(y0),
                            "width": int(x1 - x0), "height": int(y1 - y0), "rle": rle(pixels)})
    return json.dumps({
        "source-ref": PREFIX + name,
        "instance-segmentation": {"image_size": [{"width": W, "height": H, "depth": 3}],
                                  "annotations": annotations},
        "instance-segmentation-metadata": {
            "class-map": {str(i): c for i, c in enumerate(CLASSES)},
            "type": "groundtruth/instance-segmentation", "objects": [], "job-name": "t"}})


def triage_csv(tmp_path, rows):
    """rows: (filename, verdict, misplaced, missing, occluder, unlabeled_stray)."""
    body = [HEADER]
    for filename, verdict, misplaced, missing, occluder, stray in rows:
        body.append("{0},assembly48,/d/{0},{1},{2},{3},{4},high,note {0},"
                    "front_fender=1;rear_fender=1;roof=1,{5}".format(
                        filename, verdict, misplaced, missing, occluder, stray))
    path = tmp_path / "triage.csv"
    path.write_text("\n".join(body) + "\n", encoding="utf-8")
    return str(path)


def write_json(tmp_path, name, value):
    path = tmp_path / name
    path.write_text(json.dumps(value), encoding="utf-8")
    return str(path)


def record(name, items, min_outline_overlap=0.1, min_iou=0.7, confidences=None, **overrides):
    """A Result_Export record whose layout section is what the device's
    check_layout produced for its instances."""
    instances, with_ids = [], []
    for index, (cls, pixels) in enumerate(items):
        confidence = (confidences or {}).get(index, 0.9 - 0.01 * index)
        raw = {"class_label": cls, "confidence": confidence, "bounding_box": box(pixels),
               "mask_rle": rle(pixels)}
        instances.append(raw)
        with_ids.append(dict(raw, id="d{0}".format(index)))
    section = layout.run_metadata(layout.check_layout(
        with_ids, (W, H), REGIONS, min_iou=min_iou, min_outline_overlap=min_outline_overlap))
    entry = {"file": name, "execution_id": "e-" + name, "capture_id": "c-" + name,
             "status": "completed", "error": None, "layout": section,
             "timings_ms": {"model": 90, "layout": 30, "total": 900},
             "instances": instances, "detections_truncated": False}
    entry.update(overrides)
    return entry


def export(records, **header):
    value = {"workflow_id": "wf", "registration_id": "reg", "device": "mic-730ai",
             "started": "2026-11-27T10:00:00Z", "local_server_version": VERSION,
             "local_server_version_end": VERSION, "local_server_changed": False,
             "records": records}
    value.update(header)
    return value


def recommended_of(out):
    match = re.search(r"recommended_min_iou = (\d\.\d\d)", out)
    return float(match.group(1)) if match else None


KG_ROWS = [("kg0.jpg", "correct", "", "", "none", 0), ("kg1.jpg", "correct", "", "", "none", 0),
           ("kg2.jpg", "correct", "", "", "none", 0)]
FAIL_ROWS = [("swap.jpg", "incorrect", "front_fender;rear_fender", "", "none", 0),
             ("noroof.jpg", "missing", "", "roof", "none", 0)]
KG_IMAGES = [("kg0.jpg", pieces(0)), ("kg1.jpg", pieces(1)), ("kg2.jpg", pieces(3))]
FAIL_IMAGES = [("swap.jpg", pieces(0, swapped=True)), ("noroof.jpg", pieces(0, roof=False))]


def calibrate_manifest(tmp_path, capsys, images, rows, *extra):
    manifest = tmp_path / "m.manifest"
    manifest.write_text("".join(manifest_line(n, i) + "\n" for n, i in images))
    regions = write_json(tmp_path, "regions.json", REGIONS)
    code = lr.main(["--calibrate", str(manifest), "--regions", regions,
                    "--triage", triage_csv(tmp_path, rows)] + list(extra))
    return code, capsys.readouterr().out


def calibrate_results(tmp_path, capsys, value, rows, *extra, version=VERSION):
    path = write_json(tmp_path, "gray.json", value)
    regions = write_json(tmp_path, "regions.json", REGIONS)
    argv = ["--calibrate", "--results", path, "--regions", regions,
            "--triage", triage_csv(tmp_path, rows)]
    if version is not None:
        argv += ["--local-server-version", version]
    code = lr.main(argv + list(extra))
    return code, capsys.readouterr().out


# ---------------------------------------------------------------------------
# Pure helpers
# ---------------------------------------------------------------------------


def test_recommendation_rule():
    assert lr.recommend(0.95) == 0.70
    assert lr.recommend(0.79) == 0.65
    assert lr.recommend(0.80) == 0.70
    assert lr.recommend(0.42) == 0.30
    assert lr.recommend(0.10) == 0.30


def test_flipped_roof_is_the_vertical_flip_not_the_mirror():
    roof = rle(disc_pixels())
    straight, _ = layout.position_score(roof, W, H, REGIONS["roof"])
    flipped, _ = layout.position_score(roof, W, H, lr.flipped_vertically(REGIONS["roof"]))
    mirrored, _ = layout.position_score(roof, W, H, [[1.0 - x, y] for x, y in REGIONS["roof"]])
    assert straight > 0.95
    assert flipped < 0.5
    assert abs(mirrored - straight) < 0.02  # the half-disc is its own horizontal mirror


# ---------------------------------------------------------------------------
# Capture (OpenCV)
# ---------------------------------------------------------------------------


def capture_manifest(tmp_path, capsys, items, name="golden.jpg", rows=None, *extra):
    manifest = tmp_path / "m.manifest"
    manifest.write_text(manifest_line(name, items) + "\n")
    out = tmp_path / "regions.json"
    argv = ["--manifest", str(manifest), "--source-ref", PREFIX + name, "--out", str(out)]
    if rows is not None:
        argv += ["--triage", triage_csv(tmp_path, rows)]
    code = lr.main(argv + list(extra))
    return code, capsys.readouterr().out, out


@needs_cv2
def test_capture_rectangle_and_half_disc(tmp_path, capsys):
    code, out, path = capture_manifest(tmp_path, capsys, pieces(0))
    assert code == 0, out
    regions = json.loads(path.read_text())
    assert set(regions) == set(CLASSES)
    assert len(regions["rear_fender"]) == 4 and len(regions["front_fender"]) == 4
    assert "rear_fender | 4 | 1.0000" in out
    assert 3 <= len(regions["roof"]) <= 64
    roof_iou = float(re.search(r"roof \| \d+ \| (\d\.\d+)", out).group(1))
    assert roof_iou >= 0.98
    parsed, problems = layout.parse_expected_regions(path.read_text())
    assert problems == [] and parsed is not None
    score, _ = layout.position_score(rle(rect_pixels(REAR_RECT)), W, H, regions["rear_fender"])
    assert score > 0.9


@needs_cv2
def test_capture_border_touching_shape(tmp_path, capsys):
    items = [("front_fender", rect_pixels((80, 30, 120, 80))), ("rear_fender", rect_pixels((0, 30, 40, 80))),
             ("roof", disc_pixels())]
    code, out, path = capture_manifest(tmp_path, capsys, items)
    assert code == 0, out
    regions = json.loads(path.read_text())
    assert max(x for x, _ in regions["front_fender"]) <= 1.0
    assert max(y for _, y in regions["rear_fender"]) <= 1.0


@needs_cv2
def test_capture_missing_and_duplicate_label(tmp_path, capsys):
    code, out, path = capture_manifest(tmp_path, capsys, pieces(0, roof=False))
    assert code == 1 and "roof: missing" in out and not path.exists()
    items = pieces(0) + [("roof", rect_pixels((0, 0, 10, 10)))]
    code, out, path = capture_manifest(tmp_path, capsys, items)
    assert code == 1 and "roof: 2 instances" in out and not path.exists()


@needs_cv2
def test_capture_refuses_a_non_known_good_golden_sample(tmp_path, capsys):
    rows = [("golden.jpg", "correct", "", "", "hand", 0)]
    code, out, path = capture_manifest(tmp_path, capsys, pieces(0), "golden.jpg", rows)
    assert code == 1
    assert "golden.jpg is not Known_Good (Occluded_Correct)" in out and "occluder=hand" in out
    assert not path.exists()
    rows = [("golden.jpg", "correct", "", "", "none", 0)]
    code, out, path = capture_manifest(tmp_path, capsys, pieces(0), "golden.jpg", rows)
    assert code == 0 and path.exists()


def overlay_body(items, truncated=False, entry_truncated=None):
    instances = []
    for index, (cls, pixels) in enumerate(items):
        entry = {"class_label": cls, "confidence": 0.9, "bounding_box": box(pixels),
                 "mask_rle": rle(pixels)}
        if index == entry_truncated:
            entry["mask_truncated"] = True
            entry.pop("mask_rle")
        instances.append(entry)
    return {"maskImage": "aGk=", "maskBackground": None, "instances": instances,
            "detectionsTruncated": truncated}


@needs_cv2
def test_capture_from_an_overlay_with_metadata_and_with_size(tmp_path, capsys):
    overlay = write_json(tmp_path, "overlay.json", overlay_body(pieces(0)))
    metadata = write_json(tmp_path, "metadata.json",
                          {"layout": {"layout": {"frame": {"width": W, "height": H}}}})
    out = tmp_path / "r.json"
    assert lr.main(["--overlay", overlay, "--metadata", metadata, "--out", str(out)]) == 0
    first = json.loads(out.read_text())
    out.unlink()
    assert lr.main(["--overlay", overlay, "--size", "{0}x{1}".format(W, H), "--out", str(out)]) == 0
    assert json.loads(out.read_text()) == first
    top_level = write_json(tmp_path, "meta2.json", {"frame": {"width": W, "height": H}})
    out.unlink()
    assert lr.main(["--overlay", overlay, "--metadata", top_level, "--out", str(out)]) == 0
    capsys.readouterr()


@needs_cv2
def test_capture_overlay_refusals(tmp_path, capsys):
    out = tmp_path / "r.json"
    truncated = write_json(tmp_path, "t.json", overlay_body(pieces(0), truncated=True, entry_truncated=1))
    assert lr.main(["--overlay", truncated, "--size", "120x80", "--out", str(out)]) == 1
    text = capsys.readouterr().out
    assert "detectionsTruncated is true" in text and "instance 1 (rear_fender): mask_truncated" in text
    good = write_json(tmp_path, "o.json", overlay_body(pieces(0)))
    no_frame = write_json(tmp_path, "m.json", {"layout": {}})
    assert lr.main(["--overlay", good, "--metadata", no_frame, "--out", str(out)]) == 1
    assert "carries no frame size" in capsys.readouterr().out
    assert not out.exists()


def test_overlay_without_a_size_refused(tmp_path, capsys):
    good = write_json(tmp_path, "o.json", overlay_body(pieces(0)))
    assert lr.main(["--overlay", good, "--out", str(tmp_path / "r.json")]) == 1
    assert "the overlay body carries no frame size" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Calibrate from the labels
# ---------------------------------------------------------------------------


def test_calibrate_manifest_table_and_recommendation(tmp_path, capsys):
    code, out = calibrate_manifest(tmp_path, capsys, KG_IMAGES + FAIL_IMAGES, KG_ROWS + FAIL_ROWS,
                                   "--test-files", "kg1.jpg")
    assert code == 0, out
    assert "source: labels" in out
    rear_scores = [layout.position_score(rle(rect_pixels(REAR_RECT, dx)), W, H,
                                         REGIONS["rear_fender"])[0] for dx in (0, 1, 3)]
    row = next(line for line in out.splitlines() if line.startswith("rear_fender |"))
    cells = [cell.strip() for cell in row.split("|")]
    assert cells[1] == "3"
    assert float(cells[2]) == pytest.approx(min(rear_scores), abs=1e-4)
    assert float(cells[6]) == pytest.approx(rear_scores[1], abs=1e-4)  # min(test) = kg1
    assert float(cells[7]) == pytest.approx(0.0)  # swapped: the other pocket
    roof_row = next(line for line in out.splitlines() if line.startswith("roof |"))
    assert float(roof_row.split("|")[8]) < 0.5  # flipped_roof
    all_scores = []
    for _, items in KG_IMAGES:
        for cls, pixels in items:
            all_scores.append(layout.position_score(rle(pixels), W, H, REGIONS[cls])[0])
    assert recommended_of(out) == lr.recommend(min(all_scores))
    assert "(empty: every Fail_Set image fails" in out
    assert "WARNING" not in out


def test_calibrate_with_the_version_flag_on_the_manifest_form(tmp_path, capsys):
    code, out = calibrate_manifest(tmp_path, capsys, KG_IMAGES, KG_ROWS,
                                   "--local-server-version", "9.9.9")
    assert code == 0, out


def test_fail_set_listings(tmp_path, capsys):
    rows = KG_ROWS + [
        ("passes.jpg", "incorrect", "front_fender;rear_fender", "", "none", 0),
        ("halfok.jpg", "incorrect", "front_fender;rear_fender", "", "none", 0),
        ("handfail.jpg", "incorrect", "front_fender;rear_fender", "", "hand", 0)]
    # front_fender pushed half out of its pocket (misplaced), rear_fender seated.
    half = [("front_fender", rect_pixels(FRONT_RECT, 0, 10)),
            ("rear_fender", rect_pixels(REAR_RECT)), ("roof", disc_pixels())]
    images = KG_IMAGES + [("passes.jpg", pieces(0)), ("halfok.jpg", half),
                          ("handfail.jpg", pieces(0))]
    code, out = calibrate_manifest(tmp_path, capsys, images, rows)
    assert code == 1
    listing = out.split("Fail_Set true-fail check")[1].split("Fail_Set, occluded")[0]
    assert "passes.jpg | assembly48 | front_fender;rear_fender | pass | front_fender;rear_fender" in listing
    assert "halfok.jpg | assembly48 | front_fender;rear_fender | fail | rear_fender" in listing
    occluded = out.split("Fail_Set, occluded")[1].split("Occluded_Correct")[0]
    assert "handfail.jpg" in occluded and "handfail.jpg" not in listing


def test_occluded_correct_printed_without_expectation(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(assembly_triage, "STRAY_ROWS_ARE_KNOWN_GOOD", False)
    rows = KG_ROWS + [("hand.jpg", "correct", "", "", "hand", 0),
                      ("stray.jpg", "correct", "", "", "none", 1)]
    images = KG_IMAGES + [("hand.jpg", pieces(0, swapped=True)), ("stray.jpg", pieces(0))]
    code, out = calibrate_manifest(tmp_path, capsys, images, rows)
    assert code == 0, out
    block = out.split("Occluded_Correct (no expectation):")[1]
    assert "hand.jpg: fail" in block and "stray.jpg: pass" in block


def test_low_minimum_warns(tmp_path, capsys):
    images = [("kg0.jpg", pieces(0)), ("kg1.jpg", pieces(20)), ("kg2.jpg", pieces(1))]
    code, out = calibrate_manifest(tmp_path, capsys, images, KG_ROWS, "--min-iou", "0.2")
    assert "WARNING: the Known_Good minimum" in out
    assert recommended_of(out) == 0.30


def test_triage_problem_exits_1(tmp_path, capsys):
    rows = KG_ROWS + [("odd.jpg", "unsure", "", "", "none", 0)]
    code, out = calibrate_manifest(tmp_path, capsys, KG_IMAGES + [("odd.jpg", pieces(0))], rows)
    assert code == 1
    assert "triage problems (DD18)" in out and "unrecognised verdict 'unsure'" in out


# ---------------------------------------------------------------------------
# Calibrate from a Result_Export
# ---------------------------------------------------------------------------


def kg_records():
    return [record(n, items) for n, items in KG_IMAGES]


def test_results_table_from_predicted_masks(tmp_path, capsys):
    value = export(kg_records() + [record(n, i) for n, i in FAIL_IMAGES])
    code, out = calibrate_results(tmp_path, capsys, value, KG_ROWS + FAIL_ROWS)
    assert code == 0, out
    assert "source: predicted" in out
    assert recommended_of(out) is not None
    assert "re-derived verdicts: 0 disagreement(s)" in out


def test_results_duplicate_is_not_a_calibration_matter(tmp_path, capsys):
    rows = KG_ROWS + [("dup.jpg", "correct", "", "", "none", 0)]
    clean = export(kg_records())
    code, out = calibrate_results(tmp_path, capsys, clean, KG_ROWS)
    assert code == 0, out
    without = recommended_of(out)
    dup_items = pieces(12) + [("roof", disc_pixels(-2))]
    value = export(kg_records() + [record("dup.jpg", dup_items)])
    code, out = calibrate_results(tmp_path, capsys, value, rows)
    assert code == 1
    assert "duplicate -- not a calibration matter" in out
    assert "dup.jpg: roof x2" in out
    assert recommended_of(out) == without


def test_results_ignored_instance_never_best_and_outline_report(tmp_path, capsys):
    rows = KG_ROWS + [("edge.jpg", "correct", "", "", "none", 1)]
    # A higher-confidence roof outside the car outline: ignored, never scored.
    stray = ("roof", rect_pixels((0, 0, 8, 8)))
    # A front_fender lying 30 % inside the outline at an echoed 0.5: ignored, in the band.
    straddle = ("front_fender", rect_pixels((100, 0, 120, 10)) | rect_pixels((104, 40, 110, 46)))
    items = pieces(0) + [stray, straddle]
    rec = record("edge.jpg", items, min_outline_overlap=0.5, confidences={3: 0.99, 4: 0.95})
    assert [i["class_label"] for i in rec["layout"]["ignored"]] == ["roof", "front_fender"]
    value = export(kg_records() + [rec])
    code, out = calibrate_results(tmp_path, capsys, value, rows)
    assert code == 0, out
    assert "duplicate" not in out.split("check at min_iou")[0]
    report = out.split("outline report (DD23):")[1]
    # 36 of the straddling piece's 236 pixels lie inside the outline.
    assert re.search(r"Known_Good: min outline_overlap counted 0\.\d{4}, max ignored 0\.1525", report)
    band = report.split("near the car outline")[1]
    assert "edge.jpg front_fender" in band and "edge.jpg roof" not in band


def test_results_outline_zero_with_stray_known_good_refused(tmp_path, capsys):
    rows = KG_ROWS + [("edge.jpg", "correct", "", "", "none", 1)]
    value = export(kg_records() + [record("edge.jpg", pieces(0), min_outline_overlap=0)])
    code, out = calibrate_results(tmp_path, capsys, value, rows)
    assert code == 1
    assert "echo min_outline_overlap = 0" in out
    assert "label | n |" not in out


def test_results_excluded_listings(tmp_path, capsys):
    rows = KG_ROWS + [(n, "correct", "", "", "none", 0)
                      for n in ("noroof.jpg", "noframe.jpg", "null.jpg", "trunc.jpg")]
    noframe = record("noframe.jpg", pieces(0))
    noframe["layout"] = None
    value = export(kg_records() + [
        record("noroof.jpg", pieces(0, roof=False)), noframe,
        record("null.jpg", pieces(0), instances=None),
        record("trunc.jpg", pieces(0), detections_truncated=True)])
    code, out = calibrate_results(tmp_path, capsys, value, rows)
    assert code == 1
    assert "excluded (no layout.frame): noframe.jpg" in out
    assert "excluded (instances = null): null.jpg" in out
    assert "excluded (detections_truncated = true): trunc.jpg" in out
    assert "noroof.jpg: no qualifying roof" in out


def test_results_disagreement_flagged(tmp_path, capsys):
    records = kg_records()
    records[1]["layout"]["pass"] = False
    records[1]["layout"]["pieces"]["roof"]["status"] = "misplaced"
    code, out = calibrate_results(tmp_path, capsys, export(records), KG_ROWS)
    assert code == 1
    assert "re-derived verdicts: 1 disagreement(s)" in out and "kg1.jpg: exported pass=False" in out


def test_results_fail_set_piece_ok_is_the_o13_signal(tmp_path, capsys):
    rows = KG_ROWS + [("passes.jpg", "incorrect", "front_fender;rear_fender", "", "none", 0)]
    value = export(kg_records() + [record("passes.jpg", pieces(0))])
    code, out = calibrate_results(tmp_path, capsys, value, rows)
    assert code == 1
    assert "class-from-position signal (O13)" in out


# ---------------------------------------------------------------------------
# The identity gate (DD22)
# ---------------------------------------------------------------------------


def test_results_without_the_version_is_usage(tmp_path, capsys):
    with pytest.raises(SystemExit) as usage:
        calibrate_results(tmp_path, capsys, export(kg_records()), KG_ROWS, version=None)
    assert usage.value.code == 2
    assert "usage:" in capsys.readouterr().err


def test_results_bad_version_format_is_usage(tmp_path, capsys):
    with pytest.raises(SystemExit) as usage:
        calibrate_results(tmp_path, capsys, export(kg_records()), KG_ROWS, version="1.0")
    assert usage.value.code == 2


@pytest.mark.parametrize("header, text", [
    ({"local_server_version": "1.0.6"}, "local_server_version is '1.0.6', --local-server-version is '1.0.7'"),
    ({"local_server_version_end": "1.0.8"}, "local_server_version_end is '1.0.8'"),
    ({"local_server_changed": True}, "local_server_changed is True"),
])
def test_identity_gate_refuses_before_any_record(tmp_path, capsys, header, text):
    code, out = calibrate_results(tmp_path, capsys, export(kg_records(), **header), KG_ROWS)
    assert code == 1
    assert "gray.json" in out and text in out
    assert "label | n |" not in out and "source:" not in out


def test_identity_gate_names_absent_fields(tmp_path, capsys):
    value = export(kg_records())
    for name in lr.IDENTITY_FIELDS:
        value.pop(name)
    code, out = calibrate_results(tmp_path, capsys, value, KG_ROWS)
    assert code == 1
    assert ("local_server_version, local_server_version_end, local_server_changed are absent"
            in out)
    assert "label | n |" not in out


def test_capture_usage_errors(tmp_path):
    with pytest.raises(SystemExit) as usage:
        lr.main(["--manifest", "m", "--out", "x"])
    assert usage.value.code == 2
    with pytest.raises(SystemExit) as usage:
        lr.main(["--calibrate"])
    assert usage.value.code == 2
