#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Convert a DDA / Ground Truth ObjectDetection manifest into a detector
training dataset (COCO JSON or YOLO txt), split without near-duplicate
leakage.

Input manifest is JSON Lines, one entry per image, as produced by the
portal's labeling pipeline (``dda_manifest._serialize_object_detection``)
or by a SageMaker Ground Truth BoundingBox job:

    {"source-ref": "s3://bucket/img.jpg",
     "bounding-box": {"image_size": [{"width": W, "height": H, "depth": 3}],
                      "annotations": [{"class_id": 0, "left": 10, "top": 20,
                                       "width": 100, "height": 200}]},
     "bounding-box-metadata": {"class-map": {"0": "plate"},
                               "type": "groundtruth/object-detection",
                               "objects": [{"confidence": 1.0}], ...}}

The label attribute is literally ``bounding-box`` on the DDA path but is
named after the job on a native Ground Truth job, so the attribute name is
auto-detected via its ``-metadata`` sibling's ``type``.

Boxes are absolute pixels: left/top/width/height.

WHY THE SPLIT IS NOT RANDOM
---------------------------
Fixed-camera capture sessions contain many near-identical frames. A random
split puts copies of one scene into both train and test, so held-out
metrics come out inflated and you learn nothing about generalization. This
tool groups visually similar frames (same downscaled-grayscale signature
used by dedupe_frames.py, at a deliberately looser threshold) and assigns
whole groups to a single split, so no scene straddles the boundary.

Negatives (entries with zero boxes) are preserved and spread across splits:
a detector needs background images to control false positives, and they
must appear in validation too or the metric hides FP regressions.

COCO LAYOUTS
------------
``--format coco`` writes one ``_annotations.coco.json`` per split. Where the
images go and what the validation split is called depends on
``--coco-layout``:

    nested (default)                     rfdetr
    dataset/                             dataset/
      train/_annotations.coco.json         train/_annotations.coco.json
      train/images/<file>                  train/<file>
      val/_annotations.coco.json           valid/_annotations.coco.json
      val/images/<file>                    valid/<file>
      test/...                             test/...

``rfdetr`` is what RF-DETR's loader reads (it joins
``dataset_dir/<split>/<file_name>`` and expects ``train``/``valid``/``test``).
The annotation JSON content is byte-identical between layouts; only the
image paths and the validation directory name differ. The leakage-safe
grouping, negative stratification and split fractions are the same too.

Usage:
    # COCO, nested layout (most frameworks)
    python3 manifest_to_detector_dataset.py \\
        --manifest s3://ryvan-cookies/labeled/plates/output.manifest \\
        --images-dir ./frames --out ./dataset

    # COCO in the layout RF-DETR's trainer reads
    python3 manifest_to_detector_dataset.py \\
        --manifest ./output.manifest --images-dir ./frames \\
        --out ./dataset --format coco --coco-layout rfdetr

    # YOLO txt + data.yaml (ultralytics)
    python3 manifest_to_detector_dataset.py \\
        --manifest ./output.manifest --images-dir ./frames \\
        --out ./dataset --format yolo

Requires numpy, and Pillow or OpenCV (for the similarity grouping).
"""

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dedupe_frames  # noqa: E402

OBJECT_DETECTION_TYPE = "groundtruth/object-detection"

#: Frames closer than this (mean abs diff on the 64x64 signature) are treated
#: as the same scene for splitting. Deliberately looser than dedupe's default
#: of 2.0: dedupe removes only true duplicates, whereas here we want any
#: plausibly-related frames kept on the same side of the split.
DEFAULT_GROUP_THRESHOLD = 6.0

#: COCO on-disk layouts understood by --coco-layout. See the module docstring.
COCO_LAYOUTS = ("nested", "rfdetr")
DEFAULT_COCO_LAYOUT = "nested"

#: Logical split name -> directory name, per COCO layout. Splitting always
#: works in terms of train/val/test; only the rfdetr layout renames the
#: validation directory (RF-DETR's loader looks for ``valid``).
COCO_SPLIT_DIRS = {
    "nested": {"train": "train", "val": "val", "test": "test"},
    "rfdetr": {"train": "train", "val": "valid", "test": "test"},
}


# ---------------------------------------------------------------------------
# Manifest loading / parsing
# ---------------------------------------------------------------------------

def load_manifest_lines(manifest):
    """Read manifest lines from a local path or an s3:// URI."""
    if str(manifest).startswith("s3://"):
        tmp = Path(tempfile.mkdtemp(prefix="ddamanifest_")) / "manifest.jsonl"
        r = subprocess.run(["aws", "s3", "cp", str(manifest), str(tmp),
                            "--only-show-errors"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise SystemExit(f"aws s3 cp failed: {r.stderr.strip()[:600]}")
        text = tmp.read_text()
    else:
        p = Path(manifest)
        if not p.is_file():
            raise SystemExit(f"manifest not found: {p}")
        text = p.read_text()
    return [ln for ln in text.splitlines() if ln.strip()]


def detect_attribute(entry):
    """Find the bounding-box label attribute name in a manifest entry.

    Prefers a key whose ``<key>-metadata`` sibling declares the object
    detection type; falls back to the DDA-path literal 'bounding-box'.
    """
    for key, value in entry.items():
        if key.endswith("-metadata"):
            continue
        meta = entry.get(f"{key}-metadata")
        if isinstance(meta, dict) and meta.get("type") == OBJECT_DETECTION_TYPE:
            return key
    if "bounding-box" in entry:
        return "bounding-box"
    return None


def parse_entries(lines):
    """Parse manifest lines -> (records, class_names, stats).

    A record is {'source_ref', 'file_name', 'width', 'height', 'boxes'}
    where each box is (class_id, left, top, w, h) in absolute pixels.
    """
    records = []
    class_map = {}
    skipped = defaultdict(int)

    for i, line in enumerate(lines, 1):
        try:
            entry = json.loads(line)
        except ValueError:
            skipped["unparseable json"] += 1
            continue

        source_ref = entry.get("source-ref")
        if not source_ref:
            skipped["no source-ref"] += 1
            continue

        attr = detect_attribute(entry)
        if attr is None:
            # No detection attribute at all: an unlabeled image. Treat as a
            # negative only if it was explicitly labeled as such elsewhere;
            # otherwise skip, since silently training on it as background
            # would teach the model that real plates are background.
            skipped["no detection attribute (unlabeled?)"] += 1
            continue

        bb = entry.get(attr) or {}
        meta = entry.get(f"{attr}-metadata") or {}
        cmap = meta.get("class-map") or {}
        for cid, name in cmap.items():
            class_map[int(cid)] = name

        sizes = bb.get("image_size") or []
        if not sizes:
            skipped["no image_size"] += 1
            continue
        width = int(sizes[0]["width"])
        height = int(sizes[0]["height"])

        boxes = []
        bad = False
        for gt in bb.get("annotations") or []:
            try:
                cid = int(gt["class_id"])
                left, top = int(gt["left"]), int(gt["top"])
                bw, bh = int(gt["width"]), int(gt["height"])
            except (KeyError, TypeError, ValueError):
                bad = True
                break
            if bw <= 0 or bh <= 0:
                skipped["degenerate box (zero area)"] += 1
                continue
            # Clamp to bounds rather than dropping: a box a pixel or two over
            # the edge is a rounding artifact, not a bad label.
            left, top = max(0, left), max(0, top)
            bw = min(bw, width - left)
            bh = min(bh, height - top)
            if bw <= 0 or bh <= 0:
                skipped["box outside image"] += 1
                continue
            boxes.append((cid, left, top, bw, bh))
        if bad:
            skipped["malformed annotation"] += 1
            continue

        records.append({
            "source_ref": source_ref,
            "file_name": source_ref.rsplit("/", 1)[-1],
            "width": width,
            "height": height,
            "boxes": boxes,
        })

    if not class_map:
        class_map = {0: "object"}
    class_names = [class_map[k] for k in sorted(class_map)]
    return records, class_names, skipped


# ---------------------------------------------------------------------------
# Leakage-safe grouping and splitting
# ---------------------------------------------------------------------------

def group_by_similarity(records, images_dir, threshold):
    """Assign each record a group id; visually similar frames share one.

    Single-linkage against group representatives, same signature metric as
    dedupe_frames. Records whose image is missing get their own group.
    """
    import numpy as np

    dedupe_frames.BACKEND = dedupe_frames._load_backend()
    reps, rep_sigs = [], []
    groups = {}

    for rec in records:
        path = Path(images_dir) / rec["file_name"]
        if not path.is_file():
            groups[rec["file_name"]] = ("missing", len(groups))
            continue
        try:
            _, sig = dedupe_frames.read_size_and_signature(path)
        except OSError:
            groups[rec["file_name"]] = ("unreadable", len(groups))
            continue

        if rep_sigs:
            diffs = np.abs(np.stack(rep_sigs) - sig).mean(axis=1)
            nearest = int(diffs.argmin())
            if float(diffs[nearest]) <= threshold:
                groups[rec["file_name"]] = ("g", nearest)
                continue
        reps.append(rec["file_name"])
        rep_sigs.append(sig)
        groups[rec["file_name"]] = ("g", len(reps) - 1)

    return groups, len(reps)


def _assign_pool(by_group, val_frac, test_frac, assigned, counts):
    """Greedy largest-group-first assignment of one pool of groups.

    Each group goes wholly to whichever split is furthest below its target
    share, so no group (scene) is ever split. Deterministic.
    """
    total = sum(len(v) for v in by_group.values())
    if not total:
        return
    targets = {
        "train": (1.0 - val_frac - test_frac) * total,
        "val": val_frac * total,
        "test": test_frac * total,
    }
    local = {"train": 0, "val": 0, "test": 0}
    ordered = sorted(by_group.items(), key=lambda kv: (-len(kv[1]), str(kv[0])))
    for _gid, recs in ordered:
        pick = max(local, key=lambda s: (targets[s] - local[s])
                   / max(targets[s], 1e-9))
        assigned[pick].extend(recs)
        local[pick] += len(recs)
        counts[pick] += len(recs)


def split_groups(records, groups, val_frac, test_frac):
    """Assign whole groups to train/val/test, stratifying negatives.

    Two things have to hold at once:

    1. No scene may span two splits, or near-duplicate frames leak across
       the boundary and held-out metrics come out inflated. So groups are
       the unit of assignment, never individual images.
    2. Negatives (zero-box images) must appear in val and test, not just
       train. They are how false positives get measured, and a val set with
       no negatives cannot detect an FP regression -- the exact failure mode
       a low-confidence detector has. So positive-bearing groups and
       negative-only groups are balanced as separate pools against the same
       target fractions.
    """
    by_group = defaultdict(list)
    for rec in records:
        by_group[groups[rec["file_name"]]].append(rec)

    pos_pool, neg_pool = {}, {}
    for gid, recs in by_group.items():
        if any(r["boxes"] for r in recs):
            pos_pool[gid] = recs
        else:
            neg_pool[gid] = recs

    assigned = {"train": [], "val": [], "test": []}
    counts = {"train": 0, "val": 0, "test": 0}
    # Positives first so their target shares drive the overall sizes, then
    # negatives are spread over the same fractions independently.
    _assign_pool(pos_pool, val_frac, test_frac, assigned, counts)
    _assign_pool(neg_pool, val_frac, test_frac, assigned, counts)
    return assigned


# ---------------------------------------------------------------------------
# Writers
# ---------------------------------------------------------------------------

def place_image(src, dst, link):
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() or dst.is_symlink():
        dst.unlink()
    if link:
        dst.symlink_to(Path(src).resolve())
    else:
        shutil.copy2(src, dst)


def write_coco(assigned, class_names, images_dir, out, link,
               layout=DEFAULT_COCO_LAYOUT):
    """Write one ``<split_dir>/_annotations.coco.json`` per non-empty split.

    ``layout`` selects only where images land and what the validation
    directory is called (see COCO_SPLIT_DIRS); the JSON content is identical
    for every layout:

    * ``nested``: images under ``<split>/images/``, splits ``train|val|test``.
    * ``rfdetr``: images beside the JSON in ``<split>/``, validation split
      directory named ``valid`` -- the tree RF-DETR's loader reads.
    """
    if layout not in COCO_LAYOUTS:
        raise ValueError(f"unknown COCO layout {layout!r}; "
                         f"expected one of {COCO_LAYOUTS}")
    split_dirs = COCO_SPLIT_DIRS[layout]
    out = Path(out)
    for split, recs in assigned.items():
        if not recs:
            continue
        split_dir = out / split_dirs[split]
        img_dir = split_dir / "images" if layout == "nested" else split_dir
        images, annotations = [], []
        ann_id = 1
        for img_id, rec in enumerate(recs, 1):
            src = Path(images_dir) / rec["file_name"]
            if src.is_file():
                place_image(src, img_dir / rec["file_name"], link)
            images.append({
                "id": img_id,
                "file_name": rec["file_name"],
                "width": rec["width"],
                "height": rec["height"],
            })
            for cid, left, top, bw, bh in rec["boxes"]:
                annotations.append({
                    "id": ann_id,
                    "image_id": img_id,
                    "category_id": cid + 1,  # COCO ids are 1-based
                    "bbox": [left, top, bw, bh],
                    "area": bw * bh,
                    "iscrowd": 0,
                })
                ann_id += 1
        coco = {
            "info": {"description": "DDA detector dataset", "split": split},
            "licenses": [],
            "images": images,
            "annotations": annotations,
            "categories": [{"id": i + 1, "name": n}
                           for i, n in enumerate(class_names)],
        }
        ann_path = split_dir / "_annotations.coco.json"
        ann_path.parent.mkdir(parents=True, exist_ok=True)
        ann_path.write_text(json.dumps(coco, indent=2))


def write_yolo(assigned, class_names, images_dir, out, link):
    out = Path(out)
    for split, recs in assigned.items():
        if not recs:
            continue
        for rec in recs:
            src = Path(images_dir) / rec["file_name"]
            if src.is_file():
                place_image(src, out / "images" / split / rec["file_name"], link)
            lbl = out / "labels" / split / (Path(rec["file_name"]).stem + ".txt")
            lbl.parent.mkdir(parents=True, exist_ok=True)
            lines = []
            w, h = float(rec["width"]), float(rec["height"])
            for cid, left, top, bw, bh in rec["boxes"]:
                cx = (left + bw / 2.0) / w
                cy = (top + bh / 2.0) / h
                lines.append(f"{cid} {cx:.6f} {cy:.6f} {bw / w:.6f} "
                             f"{bh / h:.6f}")
            # Negatives get an explicit empty file: unambiguous background.
            lbl.write_text("\n".join(lines) + ("\n" if lines else ""))

    names = "\n".join(f"  {i}: {n}" for i, n in enumerate(class_names))
    yaml = (f"path: {Path(out).resolve()}\n"
            f"train: images/train\n"
            f"val: images/val\n"
            + ("test: images/test\n" if assigned.get("test") else "")
            + f"\nnames:\n{names}\n")
    (out / "data.yaml").write_text(yaml)


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def report(records, assigned, class_names, skipped, n_groups, net_h=1280):
    n_boxes = sum(len(r["boxes"]) for r in records)
    n_neg = sum(1 for r in records if not r["boxes"])
    print("\n=== dataset summary ===")
    print(f"  images      : {len(records)}")
    print(f"  boxes       : {n_boxes}")
    print(f"  negatives   : {n_neg} ({100.0 * n_neg / max(len(records), 1):.0f}%)")
    print(f"  classes     : {class_names}")
    print(f"  scene groups: {n_groups} (splits assigned by whole group)")

    if skipped:
        print("\n  skipped entries:")
        for reason, n in sorted(skipped.items(), key=lambda kv: -kv[1]):
            print(f"    {n:>5}  {reason}")

    print("\n=== splits (no scene spans two splits) ===")
    for split in ("train", "val", "test"):
        recs = assigned.get(split, [])
        if not recs:
            continue
        b = sum(len(r["boxes"]) for r in recs)
        neg = sum(1 for r in recs if not r["boxes"])
        print(f"  {split:<6} images={len(recs):<5} boxes={b:<6} "
              f"negatives={neg} ({100.0 * neg / len(recs):.0f}%)")

    # Box scale after the letterbox to the network input. Small boxes here
    # are the single most common cause of a detector missing objects.
    sizes = []
    for r in records:
        if not r["boxes"]:
            continue
        ratio = min(net_h / float(r["width"]), net_h / float(r["height"]))
        for _cid, _l, _t, bw, bh in r["boxes"]:
            sizes.append(min(bw, bh) * ratio)
    if sizes:
        sizes.sort()
        def pct(p):
            return sizes[min(len(sizes) - 1, int(p * len(sizes)))]
        print(f"\n=== box short side at ~{net_h}px network input ===")
        print(f"  p05={pct(0.05):.0f}px  p50={pct(0.50):.0f}px  "
              f"p95={pct(0.95):.0f}px")
        tiny = sum(1 for s in sizes if s < 32)
        if tiny:
            print(f"  WARNING: {tiny} boxes ({100.0 * tiny / len(sizes):.0f}%) "
                  "have a short side under 32px at network scale -- those are\n"
                  "  hard for any detector. Consider an ROI crop or a larger input.")


def main():
    ap = argparse.ArgumentParser(
        description="DDA/GT ObjectDetection manifest -> COCO or YOLO dataset.",
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--manifest", required=True,
                    help="Manifest JSONL (local path or s3:// URI)")
    ap.add_argument("--images-dir", required=True,
                    help="Local directory holding the images")
    ap.add_argument("--out", required=True, help="Output dataset directory")
    ap.add_argument("--format", choices=["coco", "yolo"], default="coco")
    ap.add_argument("--coco-layout", choices=list(COCO_LAYOUTS),
                    default=DEFAULT_COCO_LAYOUT,
                    help="COCO tree shape (--format coco only). 'nested' "
                         "(default) = <split>/images/<file> with splits "
                         "train|val|test; 'rfdetr' = images beside "
                         "<split>/_annotations.coco.json with the validation "
                         "split named 'valid', as RF-DETR's loader expects")
    ap.add_argument("--val-frac", type=float, default=0.15)
    ap.add_argument("--test-frac", type=float, default=0.15)
    ap.add_argument("--group-threshold", type=float,
                    default=DEFAULT_GROUP_THRESHOLD,
                    help="Similarity cutoff for scene grouping "
                         f"(default {DEFAULT_GROUP_THRESHOLD})")
    ap.add_argument("--net-input-height", type=int, default=1280,
                    help="Network input height, for the box-scale report")
    ap.add_argument("--link", action="store_true",
                    help="Symlink images instead of copying")
    args = ap.parse_args()

    if args.val_frac + args.test_frac >= 1.0:
        raise SystemExit("--val-frac + --test-frac must be < 1.0")

    lines = load_manifest_lines(args.manifest)
    print(f"read {len(lines)} manifest lines")
    records, class_names, skipped = parse_entries(lines)
    if not records:
        raise SystemExit("no usable records parsed -- check the manifest format")

    missing = [r["file_name"] for r in records
               if not (Path(args.images_dir) / r["file_name"]).is_file()]
    if missing:
        print(f"WARNING: {len(missing)} manifest images not found under "
              f"{args.images_dir} (e.g. {missing[0]}); they are kept in the\n"
              "         annotations but no image file will be placed.")

    groups, n_groups = group_by_similarity(
        records, args.images_dir, args.group_threshold)
    assigned = split_groups(records, groups, args.val_frac, args.test_frac)

    if args.format == "coco":
        write_coco(assigned, class_names, args.images_dir, args.out, args.link,
                   layout=args.coco_layout)
    else:
        write_yolo(assigned, class_names, args.images_dir, args.out, args.link)

    report(records, assigned, class_names, skipped, n_groups,
           args.net_input_height)
    layout_note = (f" ({args.coco_layout} layout)"
                   if args.format == "coco" else "")
    print(f"\nwrote {args.format} dataset{layout_note} -> "
          f"{Path(args.out).resolve()}")


if __name__ == "__main__":
    main()
