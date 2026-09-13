# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""
Unit tests for `datasets/manifest_to_detector_dataset.py` -- the DDA / Ground
Truth ObjectDetection manifest -> YOLO dataset converter that
`datasets/detection_training/train.py` runs inside the SageMaker training
container.

These replace an untracked scratch harness (`.debug_tmp/test_converter.py`)
that `docs/detection-training-gap.md` §1 cited as the converter's validation
and which was lost when that gitignored directory was removed. The converter is
the only thing standing between a labeling job and a trained detector, and it
fails silently rather than loudly: a normalization slip or a leaked scene does
not raise, it just produces a model that scores well and misses plates on
device. Hence pinning the behavior here, in a tracked test.

Semantics under test:

* **Round trip from the portal's own serializer.** Manifests are built with
  `dda_manifest.serialize_manifest(..., modality='ObjectDetection')` rather
  than hand-written fixtures, so a change to the portal's emitted shape breaks
  this test instead of breaking a GPU training job an hour in.
* **Attribute auto-detection.** The label attribute is the literal
  `bounding-box` on the DDA path but is named after the job on a native Ground
  Truth job; both resolve, the latter via its `-metadata` sibling's `type`.
* **Negatives survive as explicit empty label files.** Ultralytics reads a
  missing label file as "unlabeled" and an empty one as "background". Dropping
  negatives, or omitting the file, silently removes false-positive control.
* **Negatives are stratified across splits.** A val/test split with no
  negatives cannot measure an FP regression.
* **No similarity group spans two splits.** Fixed-camera capture sessions are
  full of near-identical frames; a random split leaks them across the boundary
  and inflates held-out metrics.
* **YOLO geometry.** Absolute pixel left/top/width/height -> normalized
  centre-format cx/cy/w/h.

The converter is pure Python + numpy/Pillow with no AWS calls, so no moto
fixtures are needed; conftest.py already places the shared layer on sys.path
for `dda_manifest`.
"""
import json
import os
import sys
from pathlib import Path

import pytest

import dda_manifest

_HERE = os.path.dirname(os.path.abspath(__file__))
_DATASETS = os.path.abspath(os.path.join(_HERE, "..", "..", "..", "datasets"))
if _DATASETS not in sys.path:
    # Appended, not prepended: this directory must never shadow a portal layer
    # or backend module for the other tests sharing the session.
    sys.path.append(_DATASETS)

import manifest_to_detector_dataset as conv  # noqa: E402

LABEL_SET = ["blue_plate"]
JOB = {"job_name": "labeling-test", "modality": "ObjectDetection",
       "label_set": LABEL_SET}


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _record(name, boxes, width=1000, height=800):
    """A canonical portal annotation record; `boxes` is [(l, t, w, h), ...]."""
    return {
        "source_ref": f"s3://bucket/frames/{name}",
        "annotation": {
            "modality": "ObjectDetection",
            "image_size": {"width": width, "height": height},
            "boxes": [{"class": LABEL_SET[0], "left": l, "top": t,
                       "width": w, "height": h} for l, t, w, h in boxes],
        },
        "human_annotated": True,
        "creation_date": "2026-09-12T23:18:19Z",
    }


def _manifest_file(tmp_path, records, job=JOB):
    """Serialize with the portal's own serializer and write JSON Lines."""
    lines = dda_manifest.serialize_manifest(records, job)
    path = tmp_path / "output.manifest"
    path.write_text("\n".join(lines) + "\n")
    return path


def _rec(name, boxes, width=1000, height=800):
    """A parsed converter record, for exercising split/write directly."""
    return {"source_ref": f"s3://bucket/frames/{name}", "file_name": name,
            "width": width, "height": height, "boxes": boxes}


def _write_image(path, gray, size=(128, 96)):
    Image = pytest.importorskip(
        "PIL.Image", reason="Pillow needed for similarity grouping")
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("L", size, color=gray).save(path, quality=95)


# ---------------------------------------------------------------------------
# Parsing: round trip from the portal serializer
# ---------------------------------------------------------------------------

def test_portal_serializer_round_trips_into_records(tmp_path):
    """The portal's emitted manifest parses back to absolute-pixel boxes."""
    records = [
        _record("a.jpg", [(100, 200, 300, 400)]),
        _record("b.jpg", [(0, 0, 10, 10), (500, 400, 100, 100)]),
        _record("c.jpg", []),  # negative
    ]
    manifest = _manifest_file(tmp_path, records)

    lines = conv.load_manifest_lines(manifest)
    parsed, class_names, skipped = conv.parse_entries(lines)

    assert class_names == ["blue_plate"]
    assert [r["file_name"] for r in parsed] == ["a.jpg", "b.jpg", "c.jpg"]
    assert parsed[0]["width"] == 1000 and parsed[0]["height"] == 800
    # (class_id, left, top, width, height), zero-based class id, pixels.
    assert parsed[0]["boxes"] == [(0, 100, 200, 300, 400)]
    assert parsed[1]["boxes"] == [(0, 0, 0, 10, 10), (0, 500, 400, 100, 100)]
    # The negative is KEPT with zero boxes, not skipped.
    assert parsed[2]["boxes"] == []
    assert not skipped


def test_detects_ground_truth_job_named_attribute():
    """A native GT job names the attribute after itself; type resolves it."""
    entry = {
        "source-ref": "s3://bucket/frames/gt.jpg",
        "plates-job": {
            "image_size": [{"width": 640, "height": 480, "depth": 3}],
            "annotations": [{"class_id": 0, "left": 5, "top": 6,
                             "width": 20, "height": 30}],
        },
        "plates-job-metadata": {
            "class-map": {"0": "plate"},
            "type": "groundtruth/object-detection",
        },
    }

    assert conv.detect_attribute(entry) == "plates-job"

    parsed, class_names, _ = conv.parse_entries([json.dumps(entry)])
    assert class_names == ["plate"]
    assert parsed[0]["boxes"] == [(0, 5, 6, 20, 30)]


def test_falls_back_to_literal_bounding_box_without_type():
    """DDA path: metadata may not declare `type`; the literal name still wins."""
    entry = {
        "source-ref": "s3://bucket/frames/dda.jpg",
        "bounding-box": {
            "image_size": [{"width": 100, "height": 100, "depth": 3}],
            "annotations": [{"class_id": 0, "left": 1, "top": 2,
                             "width": 3, "height": 4}],
        },
        "bounding-box-metadata": {"class-map": {"0": "blue_plate"}},
    }

    assert conv.detect_attribute(entry) == "bounding-box"
    parsed, _, _ = conv.parse_entries([json.dumps(entry)])
    assert parsed[0]["boxes"] == [(0, 1, 2, 3, 4)]


def test_degenerate_and_out_of_bounds_boxes_are_dropped_not_fatal():
    """A bad box loses the box, never the image."""
    entry = {
        "source-ref": "s3://bucket/frames/edge.jpg",
        "bounding-box": {
            "image_size": [{"width": 100, "height": 100, "depth": 3}],
            "annotations": [
                {"class_id": 0, "left": 0, "top": 0, "width": 0, "height": 10},
                {"class_id": 0, "left": 200, "top": 0, "width": 5,
                 "height": 5},
                {"class_id": 0, "left": 10, "top": 10, "width": 20,
                 "height": 20},
            ],
        },
        "bounding-box-metadata": {"class-map": {"0": "blue_plate"},
                                  "type": "groundtruth/object-detection"},
    }

    parsed, _, skipped = conv.parse_entries([json.dumps(entry)])

    assert len(parsed) == 1
    assert parsed[0]["boxes"] == [(0, 10, 10, 20, 20)]
    assert skipped["degenerate box (zero area)"] == 1
    assert skipped["box outside image"] == 1


# ---------------------------------------------------------------------------
# YOLO writer
# ---------------------------------------------------------------------------

def test_yolo_labels_are_normalised_centre_format(tmp_path):
    """Absolute pixel l/t/w/h -> normalized cx/cy/w/h, six decimals."""
    assigned = {"train": [_rec("a.jpg", [(0, 100, 200, 300, 400)])],
                "val": [], "test": []}

    conv.write_yolo(assigned, LABEL_SET, tmp_path / "images", tmp_path / "ds",
                    link=False)

    label = (tmp_path / "ds" / "labels" / "train" / "a.txt").read_text()
    # cx=(100+150)/1000  cy=(200+200)/800  w=300/1000  h=400/800
    assert label == "0 0.250000 0.500000 0.300000 0.500000\n"


def test_negatives_get_an_explicit_empty_label_file(tmp_path):
    """Ultralytics: missing file = unlabeled, empty file = background."""
    assigned = {"train": [_rec("neg.jpg", [])], "val": [], "test": []}

    conv.write_yolo(assigned, LABEL_SET, tmp_path / "images", tmp_path / "ds",
                    link=False)

    label = tmp_path / "ds" / "labels" / "train" / "neg.txt"
    assert label.is_file(), "negative must get a label file, not be omitted"
    assert label.read_text() == ""


def test_data_yaml_declares_classes_and_present_splits(tmp_path):
    assigned = {"train": [_rec("a.jpg", [(0, 1, 1, 5, 5)])],
                "val": [_rec("b.jpg", [(0, 1, 1, 5, 5)])],
                "test": [_rec("c.jpg", [])]}

    conv.write_yolo(assigned, LABEL_SET, tmp_path / "images", tmp_path / "ds",
                    link=False)

    yaml = (tmp_path / "ds" / "data.yaml").read_text()
    assert "train: images/train" in yaml
    assert "val: images/val" in yaml
    assert "test: images/test" in yaml
    assert "  0: blue_plate" in yaml


def test_data_yaml_omits_test_when_split_is_empty(tmp_path):
    assigned = {"train": [_rec("a.jpg", [(0, 1, 1, 5, 5)])],
                "val": [_rec("b.jpg", [(0, 1, 1, 5, 5)])], "test": []}

    conv.write_yolo(assigned, LABEL_SET, tmp_path / "images", tmp_path / "ds",
                    link=False)

    assert "test:" not in (tmp_path / "ds" / "data.yaml").read_text()


# ---------------------------------------------------------------------------
# Splitting: leakage safety and negative stratification
# ---------------------------------------------------------------------------

def test_negatives_reach_every_split():
    """Negative-only groups are balanced as their own pool, so val and test
    both get background images to measure false positives against."""
    records, groups = [], {}
    for i in range(6):  # six positive scenes
        name = f"pos{i}.jpg"
        records.append(_rec(name, [(0, 10, 10, 50, 50)]))
        groups[name] = ("g", i)
    for i in range(3):  # three negative scenes
        name = f"neg{i}.jpg"
        records.append(_rec(name, []))
        groups[name] = ("g", 100 + i)

    assigned = conv.split_groups(records, groups, 0.15, 0.15)

    for split in ("train", "val", "test"):
        negatives = [r for r in assigned[split] if not r["boxes"]]
        assert negatives, f"{split} split has no negatives"


def test_similarity_group_never_spans_two_splits(tmp_path):
    """The core leakage guarantee: whole scenes move together."""
    pytest.importorskip("numpy", reason="numpy needed for signatures")
    images = tmp_path / "frames"
    records = []
    # Three visually distinct scenes, four near-identical frames each. The
    # jitter (<= 3 grey levels) sits under the 6.0 grouping threshold; the
    # 100-level gaps between scenes sit far above it.
    for scene, base_gray in enumerate((30, 130, 230)):
        for frame in range(4):
            name = f"s{scene}f{frame}.jpg"
            _write_image(images / name, base_gray + frame)
            records.append(_rec(name, [(0, 10, 10, 40, 40)],
                                width=128, height=96))

    groups, n_groups = conv.group_by_similarity(
        records, images, conv.DEFAULT_GROUP_THRESHOLD)
    assert n_groups == 3, f"expected 3 scene groups, got {n_groups}"

    assigned = conv.split_groups(records, groups, 0.34, 0.33)

    where = {}
    for split, recs in assigned.items():
        for rec in recs:
            where.setdefault(groups[rec["file_name"]], set()).add(split)
    for group_id, splits in where.items():
        assert len(splits) == 1, (
            f"group {group_id} leaked across splits {sorted(splits)}")

    # And nothing was lost or duplicated on the way through.
    placed = [r["file_name"] for recs in assigned.values() for r in recs]
    assert sorted(placed) == sorted(r["file_name"] for r in records)


def test_full_pipeline_from_portal_manifest_to_yolo_dataset(tmp_path):
    """End to end over the path train.py actually drives."""
    pytest.importorskip("numpy", reason="numpy needed for signatures")
    images = tmp_path / "frames"
    portal_records = []
    for scene, base_gray in enumerate((40, 160)):
        for frame in range(3):
            name = f"p{scene}f{frame}.jpg"
            _write_image(images / name, base_gray + frame)
            portal_records.append(
                _record(name, [(10, 10, 40, 40)], width=128, height=96))
    manifest = _manifest_file(tmp_path, portal_records)

    lines = conv.load_manifest_lines(manifest)
    parsed, class_names, _ = conv.parse_entries(lines)
    groups, _n = conv.group_by_similarity(parsed, images,
                                          conv.DEFAULT_GROUP_THRESHOLD)
    assigned = conv.split_groups(parsed, groups, 0.5, 0.0)
    out = tmp_path / "ds"
    conv.write_yolo(assigned, class_names, images, out, link=False)

    assert (out / "data.yaml").is_file()
    for split, recs in assigned.items():
        for rec in recs:
            stem = Path(rec["file_name"]).stem
            assert (out / "images" / split / rec["file_name"]).is_file()
            assert (out / "labels" / split / f"{stem}.txt").is_file()
