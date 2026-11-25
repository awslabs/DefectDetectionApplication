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
* **COCO layouts differ only in paths.** `--coco-layout rfdetr` puts images
  beside `<split>/_annotations.coco.json` and names the validation split
  `valid` (what RF-DETR's loader reads); the annotation JSON itself is
  byte-identical to the default `nested` layout, and omitting the flag is
  `nested`.
* **Instance manifests (rfdetr-seg-support task 3.4; Req 3.3, 3.4, 3.6;
  DD10).** An InstanceSegmentation manifest from the portal serializer
  parses to the bbox record shape plus `instances`; the COCO writer copies
  each instance's Canonical_RLE counts one-to-one into
  `segmentation = {"counts": [...], "size": [h, w]}` with `area` the mask
  pixel count and never an instance annotation without `segmentation`; a
  bad RLE drops the instance with a warning naming image and instance and
  a `skipped['bad instance rle']` count; a zero-instance image is a
  negative; the split assignment is identical for a bbox manifest and an
  instance manifest over the same images; `--format yolo` refuses an
  instance manifest; a manifest mixing both line kinds is rejected naming
  both lines.

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


# ---------------------------------------------------------------------------
# COCO writer: `nested` vs `rfdetr` layout
# ---------------------------------------------------------------------------

ANN = "_annotations.coco.json"

#: Pinned independently of `conv.COCO_SPLIT_DIRS` so a drift in the module
#: constant fails here rather than being silently mirrored by the test.
RFDETR_DIRS = {"train": "train", "val": "valid", "test": "test"}
NESTED_DIRS = {"train": "train", "val": "val", "test": "test"}


def _coco_assigned():
    """Three non-empty splits: a positive, a negative, a two-box positive."""
    return {
        "train": [_rec("a.jpg", [(0, 100, 200, 300, 400)]),
                  _rec("neg.jpg", [])],
        "val": [_rec("b.jpg", [(0, 0, 0, 10, 10), (0, 500, 400, 100, 100)])],
        "test": [_rec("c.jpg", [(0, 1, 1, 5, 5)])],
    }


def _touch_images(tmp_path, assigned):
    """Placeholder image files: the COCO writer only copies, never decodes."""
    images = tmp_path / "frames"
    images.mkdir()
    for recs in assigned.values():
        for rec in recs:
            (images / rec["file_name"]).write_bytes(b"jpeg-bytes")
    return images


def _tree(root):
    """{relative posix path: bytes} for every file under `root`."""
    root = Path(root)
    return {p.relative_to(root).as_posix(): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def test_rfdetr_layout_places_images_beside_annotations(tmp_path):
    """RF-DETR joins `dataset_dir/<split>/<file_name>`: no `images/` level."""
    assigned = _coco_assigned()
    images = _touch_images(tmp_path, assigned)
    out = tmp_path / "ds"

    conv.write_coco(assigned, LABEL_SET, images, out, link=False,
                    layout="rfdetr")

    for split, recs in assigned.items():
        split_dir = out / RFDETR_DIRS[split]
        assert (split_dir / ANN).is_file(), f"{split}: annotation JSON missing"
        assert not (split_dir / "images").exists(), (
            f"{split}: rfdetr layout must not nest an images/ directory")
        for rec in recs:
            assert (split_dir / rec["file_name"]).is_file(), (
                f"{split}: {rec['file_name']} not beside {ANN}")


@pytest.mark.parametrize("layout, dirs", [("nested", NESTED_DIRS),
                                          ("rfdetr", RFDETR_DIRS)])
def test_coco_split_directory_names_per_layout(tmp_path, layout, dirs):
    """Only the validation directory is renamed, and only by `rfdetr`."""
    assigned = _coco_assigned()
    images = _touch_images(tmp_path, assigned)
    out = tmp_path / "ds"

    conv.write_coco(assigned, LABEL_SET, images, out, link=False,
                    layout=layout)

    assert {p.name for p in out.iterdir()} == set(dirs.values())
    assert conv.COCO_SPLIT_DIRS[layout] == dirs


def test_rfdetr_layout_skips_empty_splits(tmp_path):
    """An empty test split gets no `test/` directory, as in `nested`."""
    assigned = _coco_assigned()
    assigned["test"] = []
    images = _touch_images(tmp_path, assigned)
    out = tmp_path / "ds"

    conv.write_coco(assigned, LABEL_SET, images, out, link=False,
                    layout="rfdetr")

    assert {p.name for p in out.iterdir()} == {"train", "valid"}


def test_rfdetr_annotation_json_is_byte_identical_to_nested(tmp_path):
    """Same assignment -> same `_annotations.coco.json` bytes per split.

    Two classes so category ordering is observable: `categories` follow
    `class_names` order with 1-based ids, and a zero-based class id `k`
    becomes `category_id k + 1`.
    """
    class_names = ["blue_plate", "red_plate"]
    assigned = _coco_assigned()
    assigned["train"].append(_rec("d.jpg", [(1, 20, 30, 40, 50)]))
    images = _touch_images(tmp_path, assigned)
    nested_out, rfdetr_out = tmp_path / "nested", tmp_path / "rfdetr"

    conv.write_coco(assigned, class_names, images, nested_out, link=False,
                    layout="nested")
    conv.write_coco(assigned, class_names, images, rfdetr_out, link=False,
                    layout="rfdetr")

    for split in ("train", "val", "test"):
        nested_bytes = (nested_out / NESTED_DIRS[split] / ANN).read_bytes()
        rfdetr_bytes = (rfdetr_out / RFDETR_DIRS[split] / ANN).read_bytes()
        assert nested_bytes == rfdetr_bytes, f"{split}: JSON differs by layout"

        coco = json.loads(rfdetr_bytes)
        # Logical split name survives the directory rename.
        assert coco["info"]["split"] == split
        assert coco["categories"] == [{"id": 1, "name": "blue_plate"},
                                      {"id": 2, "name": "red_plate"}]
        # Bare basenames in both layouts; the loader supplies the split dir.
        assert all("/" not in img["file_name"] for img in coco["images"])
        assert all(ann["category_id"] in (1, 2)
                   for ann in coco["annotations"])

    train = json.loads((rfdetr_out / "train" / ANN).read_bytes())
    by_image = {img["id"]: img["file_name"] for img in train["images"]}
    red = [a for a in train["annotations"] if by_image[a["image_id"]] == "d.jpg"]
    assert [a["category_id"] for a in red] == [2]
    assert red[0]["bbox"] == [20, 30, 40, 50]


def test_omitted_layout_is_nested(tmp_path):
    """No `layout` argument reproduces the pre-flag tree exactly."""
    assigned = _coco_assigned()
    images = _touch_images(tmp_path, assigned)
    default_out, nested_out = tmp_path / "default", tmp_path / "nested"

    conv.write_coco(assigned, LABEL_SET, images, default_out, link=False)
    conv.write_coco(assigned, LABEL_SET, images, nested_out, link=False,
                    layout="nested")

    assert conv.DEFAULT_COCO_LAYOUT == "nested"
    default_tree = _tree(default_out)
    assert default_tree == _tree(nested_out)
    # And that tree is the historical one: images nested under <split>/images.
    assert set(default_tree) == {
        "train/_annotations.coco.json", "train/images/a.jpg",
        "train/images/neg.jpg",
        "val/_annotations.coco.json", "val/images/b.jpg",
        "test/_annotations.coco.json", "test/images/c.jpg",
    }


def test_unknown_coco_layout_is_rejected(tmp_path):
    """`write_coco` fails loudly before writing anything."""
    assigned = _coco_assigned()
    images = _touch_images(tmp_path, assigned)
    out = tmp_path / "ds"

    with pytest.raises(ValueError, match="unknown COCO layout 'bogus'"):
        conv.write_coco(assigned, LABEL_SET, images, out, link=False,
                        layout="bogus")

    assert not out.exists()


def test_cli_rejects_unknown_coco_layout(tmp_path, monkeypatch):
    """argparse `choices` gate the flag; exit code 2 is its usage error."""
    monkeypatch.setattr(sys, "argv", [
        "manifest_to_detector_dataset.py",
        "--manifest", str(tmp_path / "output.manifest"),
        "--images-dir", str(tmp_path), "--out", str(tmp_path / "ds"),
        "--format", "coco", "--coco-layout", "bogus",
    ])

    with pytest.raises(SystemExit) as exc:
        conv.main()

    assert exc.value.code == 2


def test_full_pipeline_layouts_agree_on_split_membership(tmp_path):
    """Grouping, stratification and fractions are shared; only the writer
    differs. Driving the whole path from one portal manifest through both
    layouts must give the same images per split and the same JSON."""
    pytest.importorskip("numpy", reason="numpy needed for signatures")
    images = tmp_path / "frames"
    portal_records = []
    for scene, base_gray in enumerate((40, 160)):
        for frame in range(3):
            name = f"p{scene}f{frame}.jpg"
            _write_image(images / name, base_gray + frame)
            portal_records.append(
                _record(name, [(10, 10, 40, 40)], width=128, height=96))
    # A negative scene of its own, so val gets background via stratification.
    _write_image(images / "bg.jpg", 250)
    portal_records.append(_record("bg.jpg", [], width=128, height=96))
    manifest = _manifest_file(tmp_path, portal_records)

    parsed, class_names, _ = conv.parse_entries(
        conv.load_manifest_lines(manifest))
    groups, _n = conv.group_by_similarity(parsed, images,
                                          conv.DEFAULT_GROUP_THRESHOLD)
    assigned = conv.split_groups(parsed, groups, 0.5, 0.0)
    assert assigned["train"] and assigned["val"] and not assigned["test"]

    nested_out, rfdetr_out = tmp_path / "nested", tmp_path / "rfdetr"
    conv.write_coco(assigned, class_names, images, nested_out, link=False,
                    layout="nested")
    conv.write_coco(assigned, class_names, images, rfdetr_out, link=False,
                    layout="rfdetr")

    for split in ("train", "val"):
        n_dir = nested_out / NESTED_DIRS[split]
        r_dir = rfdetr_out / RFDETR_DIRS[split]
        assert (n_dir / ANN).read_bytes() == (r_dir / ANN).read_bytes()
        nested_files = {p.name for p in (n_dir / "images").iterdir()}
        rfdetr_files = {p.name for p in r_dir.iterdir()} - {ANN}
        assert nested_files == rfdetr_files
        assert nested_files == {r["file_name"] for r in assigned[split]}
    assert not (rfdetr_out / "test").exists()


# ---------------------------------------------------------------------------
# Property: the layout flag changes paths, never content
# ---------------------------------------------------------------------------

def _assignments():
    """Arbitrary split assignments over a small, well-formed record set.

    Up to three classes so category order and the ``k -> k + 1`` id shift are
    exercised; records carry 0..3 in-bounds boxes (0 boxes = a negative);
    every record lands in exactly one of train/val/test, and any split may
    come out empty (the writer must skip it identically in both layouts).
    """
    from hypothesis import strategies as st

    def build(n_classes, dims, split_picks):
        class_names = [f"class_{i}" for i in range(n_classes)]
        assigned = {"train": [], "val": [], "test": []}
        for idx, ((width, height, raw_boxes), split) in enumerate(
                zip(dims, split_picks)):
            boxes = []
            for cid_seed, l_seed, t_seed, w_seed, h_seed in raw_boxes:
                left, top = l_seed % width, t_seed % height
                bw = 1 + w_seed % (width - left)
                bh = 1 + h_seed % (height - top)
                boxes.append((cid_seed % n_classes, left, top, bw, bh))
            assigned[split].append(
                _rec(f"img{idx}.jpg", boxes, width=width, height=height))
        return class_names, assigned

    n_records = st.integers(min_value=1, max_value=6)
    seed = st.integers(min_value=0, max_value=10_000)
    box = st.tuples(seed, seed, seed, seed, seed)
    dim = st.tuples(st.integers(min_value=8, max_value=512),
                    st.integers(min_value=8, max_value=512),
                    st.lists(box, max_size=3))
    return n_records.flatmap(lambda n: st.builds(
        build,
        st.integers(min_value=1, max_value=3),
        st.lists(dim, min_size=n, max_size=n),
        st.lists(st.sampled_from(("train", "val", "test")),
                 min_size=n, max_size=n),
    ))


def test_property_layout_never_changes_annotation_content_or_image_set():
    """For any assignment and class list, `rfdetr` and `nested` write the
    same `_annotations.coco.json` bytes per split and place the same image
    files, differing only in where those images sit and what the validation
    directory is called.

    **Validates: Requirements 2.3, 2.4**
    """
    import tempfile

    from hypothesis import given, settings

    @given(_assignments())
    @settings(deadline=None)
    def check(case):
        class_names, assigned = case
        with tempfile.TemporaryDirectory(prefix="coco-layout-") as tmp:
            tmp = Path(tmp)
            images = _touch_images(tmp, assigned)
            nested_out, rfdetr_out = tmp / "nested", tmp / "rfdetr"
            conv.write_coco(assigned, class_names, images, nested_out,
                            link=False, layout="nested")
            conv.write_coco(assigned, class_names, images, rfdetr_out,
                            link=False, layout="rfdetr")

            present = [s for s, recs in assigned.items() if recs]
            assert {p.name for p in nested_out.iterdir()} == {
                NESTED_DIRS[s] for s in present}
            assert {p.name for p in rfdetr_out.iterdir()} == {
                RFDETR_DIRS[s] for s in present}
            for split in present:
                n_dir = nested_out / NESTED_DIRS[split]
                r_dir = rfdetr_out / RFDETR_DIRS[split]
                n_bytes = (n_dir / ANN).read_bytes()
                assert n_bytes == (r_dir / ANN).read_bytes(), split
                coco = json.loads(n_bytes)
                assert coco["info"]["split"] == split
                assert coco["categories"] == [
                    {"id": i + 1, "name": n}
                    for i, n in enumerate(class_names)]
                expected = {r["file_name"] for r in assigned[split]}
                assert {p.name for p in (n_dir / "images").iterdir()} == expected
                assert {p.name for p in r_dir.iterdir()} - {ANN} == expected
                assert not (r_dir / "images").exists()

    check()


# ---------------------------------------------------------------------------
# Instance manifests -> COCO RLE (rfdetr-seg-support task 3.4; DD10)
# ---------------------------------------------------------------------------

INSTANCE_LABEL_SET = ["blue_plate", "red_plate"]
INSTANCE_JOB = {"job_name": "labeling-instances", "modality": "InstanceSegmentation",
                "label_set": INSTANCE_LABEL_SET}


def _box_mask_rle(left, top, w, h, width, height):
    """Canonical_RLE of a filled rectangle, through the portal's own encoder."""
    flat = [0] * (width * height)
    for y in range(top, top + h):
        for x in range(left, left + w):
            flat[y * width + x] = 1
    return dda_manifest.rle_encode(flat, width, height)


def _instance_record(name, boxes, width=1000, height=800):
    """A canonical InstanceSegmentation record whose instances are filled
    rectangles at `boxes` [(l, t, w, h), ...] (so the tight box is the box
    and the mask area is w * h); class alternates blue/red."""
    instances = []
    for i, (l, t, w, h) in enumerate(boxes):
        instances.append({
            "class": INSTANCE_LABEL_SET[i % 2],
            "rle": _box_mask_rle(l, t, w, h, width, height),
        })
    return {
        "source_ref": f"s3://bucket/frames/{name}",
        "annotation": {
            "modality": "InstanceSegmentation",
            "image_size": {"width": width, "height": height},
            "instances": instances,
            "classification": "anomaly" if instances else "normal",
        },
        "human_annotated": True,
        "creation_date": "2026-11-22T10:00:00Z",
    }


def _instance_manifest_file(tmp_path, records, name="instances.manifest"):
    lines = dda_manifest.serialize_manifest(records, INSTANCE_JOB)
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n")
    return path


def _instance_entry(name, annotations, width=20, height=10,
                    attr="instance-segmentation", class_map=None):
    """A hand-built instance line (for malformed RLEs the serializer refuses)."""
    return {
        "source-ref": f"s3://bucket/frames/{name}",
        attr: {
            "image_size": [{"width": width, "height": height, "depth": 3}],
            "annotations": annotations,
        },
        f"{attr}-metadata": {
            "class-map": class_map or {"0": "blue_plate"},
            "type": "groundtruth/instance-segmentation",
            "objects": [{"confidence": 1.0} for _ in annotations],
        },
    }


def test_instance_manifest_parses_to_records_with_instances_and_derived_boxes(tmp_path):
    """The portal's instance manifest parses to the bbox record shape plus
    `instances`; `boxes` is derived from them so splitting sees the same thing."""
    records = [
        _instance_record("a.jpg", [(100, 200, 300, 400)]),
        _instance_record("b.jpg", [(0, 0, 10, 10), (500, 400, 100, 100)]),
        _instance_record("c.jpg", []),  # negative
    ]
    manifest = _instance_manifest_file(tmp_path, records)

    parsed, class_names, skipped = conv.parse_entries(
        conv.load_manifest_lines(manifest))

    assert class_names == ["blue_plate", "red_plate"]
    assert [r["file_name"] for r in parsed] == ["a.jpg", "b.jpg", "c.jpg"]
    assert parsed[0]["boxes"] == [(0, 100, 200, 300, 400)]
    assert parsed[1]["boxes"] == [(0, 0, 0, 10, 10), (1, 500, 400, 100, 100)]
    assert parsed[2]["boxes"] == [] and parsed[2]["instances"] == []
    assert not skipped
    # Every instance carries its counts, parsed to ints from the manifest's rle.
    for rec in parsed:
        assert len(rec["instances"]) == len(rec["boxes"])
        for (cid, l, t, w, h, counts), box in zip(rec["instances"], rec["boxes"]):
            assert (cid, l, t, w, h) == box
            assert all(isinstance(c, int) for c in counts)
            assert sum(counts) == rec["width"] * rec["height"]
    manifest_rle = json.loads(conv.load_manifest_lines(manifest)[0])[
        "instance-segmentation"]["annotations"][0]["rle"]
    assert parsed[0]["instances"][0][5] == [int(c) for c in manifest_rle.split()]
    # A bbox manifest's records keep exactly their historical keys.
    bbox_parsed, _, _ = conv.parse_entries(
        conv.load_manifest_lines(_manifest_file(tmp_path, [_record("x.jpg", [])])))
    assert set(bbox_parsed[0]) == {"source_ref", "file_name", "width", "height", "boxes"}


def test_detects_ground_truth_job_named_instance_attribute():
    entry = _instance_entry(
        "gt.jpg", [{"class_id": 0, "left": 2, "top": 1, "width": 3, "height": 2,
                    "rle": _box_mask_rle(2, 1, 3, 2, 20, 10)}],
        attr="plates-seg")
    assert conv.detect_instance_attribute(entry) == "plates-seg"
    assert conv.detect_attribute(entry) is None

    parsed, class_names, skipped = conv.parse_entries([json.dumps(entry)])

    assert class_names == ["blue_plate"]
    assert parsed[0]["boxes"] == [(0, 2, 1, 3, 2)]
    assert not skipped


def test_bbox_entry_has_no_instance_attribute():
    entry = json.loads(dda_manifest.serialize_manifest(
        [_record("a.jpg", [(1, 1, 5, 5)])], JOB)[0])
    assert conv.detect_instance_attribute(entry) is None
    assert conv.detect_attribute(entry) == "bounding-box"


def test_coco_segmentation_is_the_manifest_rle_with_size_h_w(tmp_path):
    """Each instance's `segmentation` is the uncompressed RLE dict: the
    manifest's counts one-to-one and `size == [height, width]`; `area` is
    the mask pixel count; `iscrowd` 0; never an annotation without a mask."""
    records = [_instance_record("a.jpg", [(100, 200, 300, 200)], width=640, height=480),
               _instance_record("b.jpg", [(0, 0, 10, 10), (500, 400, 100, 50)],
                                width=640, height=480)]
    manifest = _instance_manifest_file(tmp_path, records)
    lines = conv.load_manifest_lines(manifest)
    parsed, class_names, _ = conv.parse_entries(lines)
    assigned = {"train": parsed, "val": [], "test": []}
    images = _touch_images(tmp_path, assigned)

    conv.write_coco(assigned, class_names, images, tmp_path / "ds", link=False,
                    layout="rfdetr")

    coco = json.loads((tmp_path / "ds" / "train" / ANN).read_text())
    by_file = {json.loads(ln)["source-ref"].rsplit("/", 1)[-1]: json.loads(ln)
               for ln in lines}
    by_image = {img["id"]: img["file_name"] for img in coco["images"]}
    assert len(coco["annotations"]) == 3
    for ann in coco["annotations"]:
        file_name = by_image[ann["image_id"]]
        manifest_anns = by_file[file_name]["instance-segmentation"]["annotations"]
        match = [m for m in manifest_anns
                 if [m["left"], m["top"], m["width"], m["height"]] == ann["bbox"]]
        assert len(match) == 1, ann
        expected_counts = [int(c) for c in match[0]["rle"].split()]
        assert ann["segmentation"] == {"counts": expected_counts, "size": [480, 640]}
        assert sum(ann["segmentation"]["counts"]) == 480 * 640
        assert ann["area"] == ann["bbox"][2] * ann["bbox"][3]  # filled rectangles
        assert ann["area"] == sum(expected_counts[1::2])
        assert ann["iscrowd"] == 0
        assert ann["category_id"] == match[0]["class_id"] + 1
    assert coco["categories"] == [{"id": 1, "name": "blue_plate"},
                                  {"id": 2, "name": "red_plate"}]


def test_coco_area_is_mask_pixels_not_box_area(tmp_path):
    """A diagonal mask: tight box 3x3 (area 9), mask area 3."""
    width, height = 8, 6
    flat = [0] * (width * height)
    for x, y in [(2, 1), (3, 2), (4, 3)]:
        flat[y * width + x] = 1
    rle = dda_manifest.rle_encode(flat, width, height)
    entry = _instance_entry("diag.jpg", [{"class_id": 0, "left": 2, "top": 1,
                                          "width": 3, "height": 3, "rle": rle}],
                            width=width, height=height)
    parsed, class_names, _ = conv.parse_entries([json.dumps(entry)])
    assigned = {"train": parsed, "val": [], "test": []}
    images = _touch_images(tmp_path, assigned)

    conv.write_coco(assigned, class_names, images, tmp_path / "ds", link=False,
                    layout="rfdetr")

    ann = json.loads((tmp_path / "ds" / "train" / ANN).read_text())["annotations"][0]
    assert ann["bbox"] == [2, 1, 3, 3]
    assert ann["area"] == 3
    assert ann["segmentation"]["counts"] == [int(c) for c in rle.split()]
    assert ann["segmentation"]["size"] == [height, width]


def test_bbox_records_still_write_the_historical_annotation(tmp_path):
    """A bbox manifest's COCO annotations are byte-identical to before: no
    `segmentation` key, `area` the box area."""
    assigned = _coco_assigned()
    images = _touch_images(tmp_path, assigned)

    conv.write_coco(assigned, LABEL_SET, images, tmp_path / "ds", link=False,
                    layout="rfdetr")

    train = json.loads((tmp_path / "ds" / "train" / ANN).read_text())
    assert train["annotations"] == [{
        "id": 1, "image_id": 1, "category_id": 1,
        "bbox": [100, 200, 300, 400], "area": 300 * 400, "iscrowd": 0}]


@pytest.mark.parametrize("bad_rle, reason", [
    ("", "empty rle"),
    (None, "empty rle"),
    ("3 x 197", "non-integer run length"),
    ("5 -1 196", "negative run length"),
    ("10 5", "rle counts sum to 15, expected 200 for 20x10"),
    ("200", "rle has no foreground pixels"),
    ("0 0 200", "rle has no foreground pixels"),
])
def test_bad_instance_rle_is_dropped_with_a_warning_not_fatal(capsys, bad_rle, reason):
    """Req 3.3: an instance whose RLE is empty or inconsistent with the image
    size is dropped with a warning naming the image and the instance; the
    image keeps its other instances."""
    good = {"class_id": 0, "left": 2, "top": 1, "width": 3, "height": 2,
            "rle": _box_mask_rle(2, 1, 3, 2, 20, 10)}
    bad = {"class_id": 0, "left": 0, "top": 0, "width": 1, "height": 1}
    if bad_rle is not None:
        bad["rle"] = bad_rle
    entry = _instance_entry("edge.jpg", [good, bad])

    parsed, _, skipped = conv.parse_entries([json.dumps(entry)])

    assert len(parsed) == 1
    assert parsed[0]["boxes"] == [(0, 2, 1, 3, 2)]
    assert len(parsed[0]["instances"]) == 1
    assert skipped["bad instance rle"] == 1
    out = capsys.readouterr().out
    assert f"WARN: edge.jpg instance 1: {reason}" in out


def test_image_left_with_zero_instances_is_a_negative(capsys, tmp_path):
    """Every instance bad -> the record stays, with no instances, and the
    split/writer treat it exactly as a zero-box image."""
    entry = _instance_entry("allbad.jpg", [
        {"class_id": 0, "left": 0, "top": 0, "width": 1, "height": 1, "rle": "1 1"},
        {"class_id": 0, "left": 0, "top": 0, "width": 1, "height": 1, "rle": "200"},
    ])

    parsed, class_names, skipped = conv.parse_entries([json.dumps(entry)])

    assert len(parsed) == 1
    assert parsed[0]["boxes"] == [] and parsed[0]["instances"] == []
    assert skipped["bad instance rle"] == 2
    out = capsys.readouterr().out
    assert "WARN: allbad.jpg instance 0:" in out
    assert "WARN: allbad.jpg instance 1:" in out

    # Negative-only group pool, as for a zero-box image.
    assigned = conv.split_groups(parsed, {"allbad.jpg": ("g", 0)}, 0.15, 0.15)
    placed = [r for recs in assigned.values() for r in recs]
    assert placed == parsed
    images = _touch_images(tmp_path, assigned)
    conv.write_coco(assigned, class_names, images, tmp_path / "ds", link=False,
                    layout="rfdetr")
    written = [json.loads((tmp_path / "ds" / d / ANN).read_text())
               for d in ("train", "valid", "test")
               if (tmp_path / "ds" / d / ANN).is_file()]
    assert len(written) == 1
    assert written[0]["images"][0]["file_name"] == "allbad.jpg"
    assert written[0]["annotations"] == []


def test_instance_and_bbox_manifests_over_the_same_images_split_identically(tmp_path):
    """Req 3.4: grouping, negative stratification and fractions are shared; the
    format flag changes only annotation content. Same images, same seed."""
    pytest.importorskip("numpy", reason="numpy needed for signatures")
    images = tmp_path / "frames"
    bbox_records, instance_records = [], []
    for scene, base_gray in enumerate((40, 110, 180)):
        for frame in range(3):
            name = f"p{scene}f{frame}.jpg"
            _write_image(images / name, base_gray + frame)
            bbox_records.append(_record(name, [(10, 10, 40, 40)], width=128, height=96))
            instance_records.append(
                _instance_record(name, [(10, 10, 40, 40)], width=128, height=96))
    for i, gray in enumerate((230, 250)):  # two negative scenes
        name = f"bg{i}.jpg"
        _write_image(images / name, gray)
        bbox_records.append(_record(name, [], width=128, height=96))
        instance_records.append(_instance_record(name, [], width=128, height=96))

    def run(manifest):
        parsed, _, skipped = conv.parse_entries(conv.load_manifest_lines(manifest))
        assert not skipped
        groups, n = conv.group_by_similarity(parsed, images, conv.DEFAULT_GROUP_THRESHOLD)
        assigned = conv.split_groups(parsed, groups, 0.34, 0.33)
        return groups, n, {s: [r["file_name"] for r in recs] for s, recs in assigned.items()}

    bbox_groups, bbox_n, bbox_split = run(_manifest_file(tmp_path, bbox_records))
    inst_groups, inst_n, inst_split = run(
        _instance_manifest_file(tmp_path, instance_records))

    assert bbox_groups == inst_groups and bbox_n == inst_n
    assert bbox_split == inst_split
    assert all(bbox_split.values()), bbox_split  # every split populated


def test_yolo_refuses_an_instance_manifest(tmp_path):
    """DD10: no silent box-only dataset; the error names the format to use."""
    manifest = _instance_manifest_file(
        tmp_path, [_instance_record("a.jpg", [(1, 1, 5, 5)]),
                   _instance_record("neg.jpg", [])])
    parsed, class_names, _ = conv.parse_entries(conv.load_manifest_lines(manifest))
    assigned = {"train": parsed, "val": [], "test": []}
    images = _touch_images(tmp_path, assigned)

    with pytest.raises(SystemExit) as exc:
        conv.write_yolo(assigned, class_names, images, tmp_path / "ds", link=False)

    assert str(exc.value) == ("error: --format yolo cannot carry instance masks; "
                              "use --format coco --coco-layout rfdetr")
    assert not (tmp_path / "ds").exists()


def test_cli_yolo_refusal_exits_with_the_error(tmp_path, monkeypatch):
    pytest.importorskip("numpy", reason="numpy needed for signatures")
    images = tmp_path / "frames"
    _write_image(images / "a.jpg", 40)
    manifest = _instance_manifest_file(
        tmp_path, [_instance_record("a.jpg", [(10, 10, 40, 40)], width=128, height=96)])
    monkeypatch.setattr(sys, "argv", [
        "manifest_to_detector_dataset.py", "--manifest", str(manifest),
        "--images-dir", str(images), "--out", str(tmp_path / "ds"),
        "--format", "yolo",
    ])

    with pytest.raises(SystemExit) as exc:
        conv.main()

    assert "--format yolo cannot carry instance masks" in str(exc.value)


def test_manifest_mixing_bbox_and_instance_lines_is_rejected_naming_both(tmp_path):
    bbox_line = dda_manifest.serialize_manifest([_record("a.jpg", [(1, 1, 5, 5)])], JOB)[0]
    instance_line = dda_manifest.serialize_manifest(
        [_instance_record("b.jpg", [(1, 1, 5, 5)])], INSTANCE_JOB)[0]

    with pytest.raises(SystemExit) as exc:
        conv.parse_entries([bbox_line, "", instance_line])
    assert "line 1 is bbox" in str(exc.value) and "line 3 is instance" in str(exc.value)

    with pytest.raises(SystemExit) as exc:
        conv.parse_entries([instance_line, bbox_line])
    assert "line 1 is instance" in str(exc.value) and "line 2 is bbox" in str(exc.value)


def test_instance_manifest_end_to_end_into_the_rfdetr_layout(tmp_path):
    """The path train_rfdetr_seg.py drives: portal instance manifest ->
    parse -> group -> split -> `--coco-layout rfdetr`, every annotation masked."""
    pytest.importorskip("numpy", reason="numpy needed for signatures")
    images = tmp_path / "frames"
    records = []
    for scene, base_gray in enumerate((40, 160)):
        for frame in range(3):
            name = f"p{scene}f{frame}.jpg"
            _write_image(images / name, base_gray + frame)
            records.append(_instance_record(name, [(10, 10, 40, 40), (60, 50, 20, 20)],
                                            width=128, height=96))
    _write_image(images / "bg.jpg", 250)
    records.append(_instance_record("bg.jpg", [], width=128, height=96))
    manifest = _instance_manifest_file(tmp_path, records)

    parsed, class_names, skipped = conv.parse_entries(conv.load_manifest_lines(manifest))
    assert not skipped
    groups, _n = conv.group_by_similarity(parsed, images, conv.DEFAULT_GROUP_THRESHOLD)
    assigned = conv.split_groups(parsed, groups, 0.5, 0.0)
    out = tmp_path / "ds"
    conv.write_coco(assigned, class_names, images, out, link=False, layout="rfdetr")

    assert {p.name for p in out.iterdir()} == {"train", "valid"}
    total = 0
    for split_dir in ("train", "valid"):
        coco = json.loads((out / split_dir / ANN).read_text())
        for img in coco["images"]:
            assert (out / split_dir / img["file_name"]).is_file()
            assert (img["width"], img["height"]) == (128, 96)
        for ann in coco["annotations"]:
            assert "segmentation" in ann
            assert ann["segmentation"]["size"] == [96, 128]
            assert sum(ann["segmentation"]["counts"]) == 96 * 128
            assert ann["iscrowd"] == 0
        total += len(coco["annotations"])
    assert total == 12
