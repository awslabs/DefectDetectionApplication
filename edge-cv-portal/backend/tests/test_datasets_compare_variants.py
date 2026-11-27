# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for `datasets/compare_variants.py` -- the grayscale / colour A/B
table (car-assembly-check task 15.1; design §12 step 4, DD12, DD18, DD22,
DD23).

Synthetic Result_Exports on a 40 x 30 frame (rectangle pieces; each
record's `layout` section is what the device's `check_layout` produced at
that variant's threshold), a synthetic labeled manifest, two
`training_metadata.json` files and a synthetic triage CSV in the real
vocabulary. M2's resampling runs through OpenCV where it is installed and
through Pillow otherwise (the Portal venv).
"""
import json
import os
import sys

import pytest

import dda_manifest
from workflow_core.analytics import layout

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATASETS = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "datasets"))
if _DATASETS not in sys.path:
    sys.path.append(_DATASETS)

import compare_variants as cv  # noqa: E402

FW, FH = 40, 30
VERSION = "1.0.7"
CLASSES = ["front_fender", "rear_fender", "roof"]
RECTS = {"rear_fender": (2, 15, 18, 28), "front_fender": (22, 15, 38, 28), "roof": (10, 2, 30, 12)}
HEADER = ("filename,set,source_path,verdict,misplaced_pieces,missing_pieces,"
          "occluder,confidence,notes,piece_counts,unlabeled_stray")


def norm(rect):
    x0, y0, x1, y1 = rect
    return [[x0 / FW, y0 / FH], [x1 / FW, y0 / FH], [x1 / FW, y1 / FH], [x0 / FW, y1 / FH]]


REGIONS = {label: norm(rect) for label, rect in RECTS.items()}


def pix(rect, dx=0, dy=0, width=FW, height=FH):
    x0, y0, x1, y1 = rect
    return {(x + dx, y + dy) for x in range(x0, x1) for y in range(y0, y1)
            if 0 <= x + dx < width and 0 <= y + dy < height}


def rle(pixels, width=FW, height=FH):
    mask = [0] * (width * height)
    for x, y in pixels:
        mask[y * width + x] = 1
    return dda_manifest.rle_encode(mask, width, height)


def box(pixels):
    xs, ys = [p[0] for p in pixels], [p[1] for p in pixels]
    return [float(min(xs)), float(min(ys)), float(max(xs) + 1), float(max(ys) + 1)]


def correct(dx=0):
    return [(label, pix(rect, dx)) for label, rect in sorted(RECTS.items())]


def swapped():
    return [("front_fender", pix(RECTS["rear_fender"])), ("rear_fender", pix(RECTS["front_fender"])),
            ("roof", pix(RECTS["roof"]))]


def record(name, items, threshold, overlap=0.1, timings=None, frame=True, **overrides):
    instances, with_ids = [], []
    for index, (cls, pixels) in enumerate(items):
        raw = {"class_label": cls, "confidence": 0.9 - 0.01 * index, "bounding_box": box(pixels),
               "mask_rle": rle(pixels)}
        instances.append(raw)
        with_ids.append(dict(raw, id="d{0}".format(index)))
    section = layout.run_metadata(layout.check_layout(
        with_ids, (FW, FH), REGIONS, min_iou=threshold, min_outline_overlap=overlap))
    if not frame:
        section["frame"] = None
    entry = {"file": name, "execution_id": "e", "capture_id": "c", "status": "completed",
             "error": None, "layout": section,
             "timings_ms": dict(timings or {"gray": 400, "model": 90, "layout": 30, "total": 900}),
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


def manifest_line(name, items, width=FW, height=FH):
    annotations = []
    for cls, pixels in items:
        x0, y0, x1, y1 = box(pixels)
        annotations.append({"class_id": CLASSES.index(cls), "left": int(x0), "top": int(y0),
                            "width": int(x1 - x0), "height": int(y1 - y0),
                            "rle": rle(pixels, width, height)})
    return json.dumps({
        "source-ref": "s3://bucket/assembly-photos/" + name,
        "instance-segmentation": {"image_size": [{"width": width, "height": height, "depth": 3}],
                                  "annotations": annotations},
        "instance-segmentation-metadata": {"class-map": {str(i): c for i, c in enumerate(CLASSES)},
                                           "type": "groundtruth/instance-segmentation"}})


ROWS = [("kg0.jpg", "correct", "", "", "none", 0), ("kg1.jpg", "correct", "", "", "none", 0),
        ("swap.jpg", "incorrect", "front_fender;rear_fender", "", "none", 0),
        ("handswap.jpg", "incorrect", "front_fender;rear_fender", "", "hand", 0),
        ("handok.jpg", "correct", "", "", "hand", 0)]
IMAGES = {"kg0.jpg": correct(), "kg1.jpg": correct(1), "swap.jpg": swapped(),
          "handswap.jpg": correct(), "handok.jpg": correct()}


def write_inputs(tmp_path, gray_records, color_records, rows=ROWS, labels=None,
                 gray_header=None, color_header=None, gray_map=0.60, color_map=0.60):
    paths = {}

    def put(name, value):
        path = tmp_path / name
        path.write_text(value if isinstance(value, str) else json.dumps(value), encoding="utf-8")
        paths[name] = str(path)

    put("gray.json", export(gray_records, **(gray_header or {})))
    put("color.json", export(color_records, **(color_header or {})))
    for name, value in (("gmeta.json", gray_map), ("cmeta.json", color_map)):
        put(name, {"metrics": {"test_map50": 0.9, "test_map50_95": 0.7, "test_mask_map50": 0.85,
                               "test_mask_map50_95": value}})
    put("regions.json", REGIONS)
    labels = labels if labels is not None else [manifest_line(n, i) for n, i in IMAGES.items()]
    put("m.manifest", "".join(item + "\n" for item in labels))
    body = [HEADER] + ["{0},assembly48,/d,{1},{2},{3},{4},high,note {0},"
                       "front_fender=1;rear_fender=1;roof=1,{5}".format(*row) for row in rows]
    put("triage.csv", "\n".join(body) + "\n")
    return paths


def run(tmp_path, capsys, paths, *extra, version=VERSION, gray_iou="0.70", color_iou="0.70"):
    out = tmp_path / "table.md"
    argv = ["--gray-meta", paths["gmeta.json"], "--color-meta", paths["cmeta.json"],
            "--gray-results", paths["gray.json"], "--color-results", paths["color.json"],
            "--labeled", paths["m.manifest"], "--regions", paths["regions.json"],
            "--triage", paths["triage.csv"], "--gray-min-iou", gray_iou,
            "--color-min-iou", color_iou, "--test-files", "kg1.jpg,swap.jpg", "--out", str(out)]
    if version is not None:
        argv += ["--local-server-version", version]
    code = cv.main(argv + list(extra))
    return code, capsys.readouterr().out, out


def both(threshold=0.7, **kwargs):
    return [record(n, i, threshold, **kwargs) for n, i in IMAGES.items()]


def row(text, prefix):
    return next(line for line in text.splitlines() if line.startswith(prefix))


# ---------------------------------------------------------------------------
# Table and metrics
# ---------------------------------------------------------------------------


def test_table_shape_and_each_metric(tmp_path, capsys):
    gray = both()
    gray[1]["timings_ms"]["total"] = 1100
    paths = write_inputs(tmp_path, gray, both(), color_map=0.55)
    code, out, table = run(tmp_path, capsys, paths)
    assert code == 0, out
    text = table.read_text()
    assert "| metric | grayscale | colour |" in text
    assert "| M1 test_mask_map50_95 | 0.6000 | 0.5500 |" in text
    assert "| M3 false-fail (Known_Good) | 0/2 = 0.0000 | 0/2 = 0.0000 |" in text
    # handswap carries correct pieces: the device passed it.
    assert "| M3 false-pass (Fail_Set) | 1/2 = 0.5000 | 1/2 = 0.5000 |" in text
    assert "| M3 per-piece agreement (Fail_Set, no occluder) | 1/1 | 1/1 |" in text
    assert "| M3 per-piece agreement (Fail_Set, occluded) | 0/1 | 0/1 |" in text
    assert "handok.jpg: pass" in row(text, "| M3 Occluded_Correct verdicts")
    assert "handswap.jpg: front_fender reported ok (O13 signal)" in row(text, "| M3 Fail_Set pieces reported ok")
    assert "| M4 timings_ms.total | median 900.0 / p95 1100.0 | median 900.0 / p95 900.0 |" in text
    assert "| M4 timings_ms.gray | median 400.0 / p95 400.0 | - |" in text
    assert "| M2 grid | 40x30 | 40x30 |" in text
    assert "| M2 rear_fender (all) | mean 1.0000 / min 1.0000 (n=5)" in text
    assert "| M2 rear_fender (test) | mean 1.0000 / min 1.0000 (n=2)" in text
    assert "Ranked order: 1. grayscale, 2. colour (decided by: M1 mask mAP50-95)" in text
    assert "| re-derivation disagreements | 0 | 0 |" in text


def test_per_variant_thresholds_in_the_re_derivation(tmp_path, capsys):
    # kg1 is shifted by one pixel: 15/17 = 0.88 for the fenders. At 0.9 they
    # are misplaced; the colour export was produced at 0.9, the grey at 0.7.
    paths = write_inputs(tmp_path, both(0.7), both(0.9))
    code, out, table = run(tmp_path, capsys, paths, gray_iou="0.70", color_iou="0.90")
    assert code == 0, out
    text = table.read_text()
    assert "| M3 false-fail (Known_Good) | 0/2 = 0.0000 | 1/2 = 0.5000 |" in text
    assert "Ranked order: 1. grayscale, 2. colour (decided by: false-fail rate)" in text


def test_disagreement_flag(tmp_path, capsys):
    paths = write_inputs(tmp_path, both(0.7), both(0.7))
    code, out, table = run(tmp_path, capsys, paths, gray_iou="0.70", color_iou="0.90")
    assert code == 1
    assert "kg1.jpg: exported pass=True" in row(table.read_text(), "| re-derivation disagreements")


def test_missing_file_mark(tmp_path, capsys):
    rows = ROWS + [("extra.jpg", "correct", "", "", "none", 0)]
    gray = both() + [record("extra.jpg", correct(), 0.7)]
    color = both() + [record("extra.jpg", correct(), 0.7)]
    paths = write_inputs(tmp_path, gray, color, rows=rows)
    code, out, table = run(tmp_path, capsys, paths)
    assert code == 0, out
    assert "extra.jpg: no manifest match" in row(table.read_text(), "| marks")


def test_null_instances_truncation_and_missing_timing_marked(tmp_path, capsys):
    gray = both()
    gray[0]["instances"] = None
    gray[1]["detections_truncated"] = True
    gray[2]["timings_ms"].pop("layout")
    paths = write_inputs(tmp_path, gray, both())
    code, out, table = run(tmp_path, capsys, paths)
    marks = row(table.read_text(), "| marks")
    assert "kg0.jpg: instances = null" in marks
    assert "kg1.jpg: detections_truncated = true" in marks
    assert "swap.jpg: timings_ms.layout missing" in marks


def test_file_set_mismatch(tmp_path, capsys):
    paths = write_inputs(tmp_path, both(), both()[:-1])
    code, out, _ = run(tmp_path, capsys, paths)
    assert code == 1
    assert "file-set mismatch: only grayscale ['handok.jpg']" in out


def test_metric_flags(tmp_path, capsys):
    paths = write_inputs(tmp_path, both(), both())
    code, out, table = run(tmp_path, capsys, paths, "--no-m2", "--no-m4", "--label", "job=r2")
    assert code == 0, out
    text = table.read_text()
    assert "| M2" not in text and "| M4" not in text
    assert "| M1 test_map50 |" in text and "| M3 false-fail" in text
    assert "- job=r2" in text


def test_tie_keeps_grayscale(tmp_path, capsys):
    paths = write_inputs(tmp_path, both(), both(), gray_map=0.60, color_map=0.615)
    code, out, table = run(tmp_path, capsys, paths)
    assert code == 0, out
    assert "Ranked order: 1. grayscale, 2. colour (decided by: tie" in table.read_text()


def test_m4_decides_after_a_map_tie(tmp_path, capsys):
    color = both()
    for item in color:
        item["timings_ms"]["total"] = 700
    paths = write_inputs(tmp_path, both(), color, gray_map=0.60, color_map=0.61)
    code, out, table = run(tmp_path, capsys, paths)
    assert "Ranked order: 1. colour, 2. grayscale (decided by: M4 total median)" in table.read_text()


# ---------------------------------------------------------------------------
# M2 on one grid
# ---------------------------------------------------------------------------


def test_m2_resamples_the_label_to_the_record_frame(tmp_path, capsys):
    # The label is at 80 x 60, the record's frame is 40 x 30.
    label_items = [(label, pix(tuple(2 * v for v in rect), width=80, height=60))
                   for label, rect in sorted(RECTS.items())]
    labels = [manifest_line(n, i) for n, i in IMAGES.items() if n != "kg0.jpg"]
    labels.append(manifest_line("kg0.jpg", label_items, 80, 60))
    gray = both()
    # kg0's predicted roof covers 15 of the 20 label columns: IoU 0.75.
    gray[0] = record("kg0.jpg", [("front_fender", pix(RECTS["front_fender"])),
                                 ("rear_fender", pix(RECTS["rear_fender"])),
                                 ("roof", pix((10, 2, 25, 12)))], 0.7)
    paths = write_inputs(tmp_path, gray, both(), labels=labels)
    code, out, table = run(tmp_path, capsys, paths)
    text = table.read_text()
    assert "| M2 roof (all) | mean 0.9500 / min 0.7500 (n=5)" in text


def test_m2_iou_analytic():
    label = [rle(pix((20, 10, 60, 40), width=80, height=60), 80, 60)]
    instance = {"mask_rle": rle(pix((10, 5, 25, 20)))}
    assert cv.m2_iou(label, (80, 60), instance, (FW, FH)) == pytest.approx(0.75)


def test_record_without_frame_not_comparable_but_counted_for_m3(tmp_path, capsys):
    gray = both()
    gray[0] = record("kg0.jpg", correct(), 0.7, frame=False)
    gray[0]["layout"]["pass"] = False
    paths = write_inputs(tmp_path, gray, both())
    code, out, table = run(tmp_path, capsys, paths)
    text = table.read_text()
    assert "kg0.jpg roof" in row(text, "| M2 not comparable")
    assert "| M2 roof (all) | mean 1.0000 / min 1.0000 (n=4)" in text
    assert "| M3 false-fail (Known_Good) | 1/2 = 0.5000 |" in text


# ---------------------------------------------------------------------------
# The identity gate (DD22)
# ---------------------------------------------------------------------------


def test_version_absent_is_usage(tmp_path, capsys):
    paths = write_inputs(tmp_path, both(), both())
    with pytest.raises(SystemExit) as usage:
        run(tmp_path, capsys, paths, version=None)
    assert usage.value.code == 2


@pytest.mark.parametrize("gray_header, color_header, texts", [
    ({"local_server_version": "1.0.6"}, None, ["gray.json: local_server_version is '1.0.6'"]),
    (None, {"local_server_version_end": "1.0.8"}, ["color.json: local_server_version_end is '1.0.8'"]),
    ({"local_server_changed": True}, None, ["gray.json: local_server_changed is True"]),
    (None, {"local_server_changed": True}, ["color.json: local_server_changed is True"]),
    ({"local_server_version": "1.0.5", "local_server_version_end": "1.0.5"},
     {"local_server_version": "1.0.9", "local_server_version_end": "1.0.9"},
     ["gray.json: local_server_version is '1.0.5'", "color.json: local_server_version is '1.0.9'"]),
])
def test_identity_gate_refuses_before_any_metric(tmp_path, capsys, gray_header, color_header, texts):
    paths = write_inputs(tmp_path, both(), both(), gray_header=gray_header, color_header=color_header)
    code, out, table = run(tmp_path, capsys, paths)
    assert code == 1
    for text in texts:
        assert text in out
    assert not table.exists()


def test_identity_gate_names_absent_fields(tmp_path, capsys):
    paths = write_inputs(tmp_path, both(), both())
    value = json.loads(open(paths["gray.json"]).read())
    for name in ("local_server_version", "local_server_version_end", "local_server_changed"):
        value.pop(name)
    open(paths["gray.json"], "w").write(json.dumps(value))
    code, out, table = run(tmp_path, capsys, paths)
    assert code == 1
    assert "local_server_version, local_server_version_end, local_server_changed are absent" in out
    assert not table.exists()


# ---------------------------------------------------------------------------
# The outline rule (O19, DD23)
# ---------------------------------------------------------------------------


def outline_items(inside):
    """The correct pieces plus a roof whose mask has ``inside`` of its 10
    pixels inside the car outline (the roof region's top-left corner)."""
    outside = pix((0, 0, 10 - inside, 1))
    within = pix((10, 2, 10 + inside, 3))
    return correct() + [("roof", outside | within)]


def test_echoed_min_outline_overlap_is_used(tmp_path, capsys):
    gray = both(overlap=0.3)
    gray[0] = record("kg0.jpg", outline_items(2), 0.7, overlap=0.3)  # 0.2 < 0.3: ignored
    gray[1] = record("kg1.jpg", outline_items(3), 0.7, overlap=0.3)  # 0.3: counted
    assert gray[0]["layout"]["pass"] is True and gray[0]["layout"]["ignored"]
    assert gray[1]["layout"]["pieces"]["roof"]["status"] == "duplicate"
    paths = write_inputs(tmp_path, gray, both())
    code, out, table = run(tmp_path, capsys, paths)
    assert code == 0, out
    text = table.read_text()
    assert "| re-derivation disagreements | 0 | 0 |" in text
    assert "| M3 false-fail (Known_Good) | 1/2 = 0.5000 |" in text


def test_outline_zero_with_a_stray_known_good_row_refused(tmp_path, capsys):
    rows = ROWS + [("stray.jpg", "correct", "", "", "none", 1)]
    gray = both(overlap=0)
    paths = write_inputs(tmp_path, gray, both(), rows=rows)
    code, out, table = run(tmp_path, capsys, paths)
    assert code == 1
    assert "echo min_outline_overlap = 0" in out
    assert not table.exists()
