#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""
Drop near-duplicate frames from a device capture set before labeling.

A fixed-camera DDA capture session produces runs of frames where nothing
in the scene moved. Those are worthless as training examples -- worse than
worthless, because labeling them costs money and they inflate held-out
metrics when a random split puts copies of one scene on both sides.

Observed on a real blue-plate session: of 17 captured frames, 8 shared a
bounding box to within 2 pixels. The set was really 4-5 distinct scenes.

This tool compares every candidate against all previously kept frames on a
downscaled grayscale signature, and keeps a frame only when it differs from
each of them by more than --threshold mean absolute difference (0-255
scale). Comparing against all kept frames (not just the previous one) also
catches a scene the operator returned to later in the session.

It additionally reports capture-resolution consistency, because a detector
dataset must be shot at ONE resolution and aspect ratio -- the same one
production will run. Mixed aspect ratios mean mixed letterbox geometry,
which is variation that hurts rather than helps.

Usage:
    # Report only: see the distance distribution and pick a threshold
    python3 dedupe_frames.py --images-dir ./captures --report-only

    # Copy the deduped set out
    python3 dedupe_frames.py --images-dir ./captures --out ./deduped

    # Symlink instead of copying (large frames)
    python3 dedupe_frames.py --images-dir ./captures --out ./deduped --link

    # Stricter / looser
    python3 dedupe_frames.py --images-dir ./captures --out ./deduped --threshold 4.0
"""

import argparse
import logging
import os
import shutil
import sys
from collections import Counter
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
logger = logging.getLogger(__name__)

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".tiff", ".tif"}

#: Signature size. Small enough that lighting flicker and sensor noise wash
#: out, large enough that a plate moving a few centimetres still registers.
SIGNATURE_SIZE = 64

#: Default mean-absolute-difference threshold on the 0-255 signature.
#: Frames closer than this to an already-kept frame are treated as the same
#: scene. 2.0 rejects "operator did not touch anything" runs while keeping
#: genuine small repositionings.
DEFAULT_THRESHOLD = 2.0


# ---------------------------------------------------------------------------
# Image backend (PIL preferred, OpenCV fallback) -- keeps this script usable
# wherever one of the two is already present.
# ---------------------------------------------------------------------------

def _load_backend():
    try:
        from PIL import Image  # noqa: F401
        return "pil"
    except ImportError:
        pass
    try:
        import cv2  # noqa: F401
        return "cv2"
    except ImportError:
        pass
    logger.error(
        "Needs Pillow or OpenCV. Install one:\n"
        "    pip install Pillow\n"
        "    pip install opencv-python-headless"
    )
    sys.exit(2)


BACKEND = None


def read_size_and_signature(path):
    """Return ((width, height), signature) for one image.

    The signature is a flat float array of length SIGNATURE_SIZE**2 holding
    a downscaled grayscale copy on the 0-255 scale.
    """
    import numpy as np

    if BACKEND == "pil":
        from PIL import Image
        with Image.open(path) as im:
            size = im.size  # (w, h)
            gray = im.convert("L").resize(
                (SIGNATURE_SIZE, SIGNATURE_SIZE), Image.BILINEAR)
            sig = np.asarray(gray, dtype=np.float32).reshape(-1)
        return size, sig

    import cv2
    img = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
    if img is None:
        raise OSError(f"could not read image: {path}")
    h, w = img.shape[:2]
    small = cv2.resize(img, (SIGNATURE_SIZE, SIGNATURE_SIZE),
                       interpolation=cv2.INTER_AREA)
    return (w, h), small.astype("float32").reshape(-1)


def find_images(images_dir, exclude_substrings):
    """Image files in images_dir, sorted by mtime so runs stay contiguous."""
    found = []
    for p in Path(images_dir).iterdir():
        if not p.is_file() or p.suffix.lower() not in IMAGE_EXTENSIONS:
            continue
        name = p.name.lower()
        if any(s and s.lower() in name for s in exclude_substrings):
            continue
        found.append(p)
    found.sort(key=lambda p: (p.stat().st_mtime, p.name))
    return found


def dedupe(paths, threshold):
    """Greedy keep-if-far-from-all-kept.

    Returns (kept, dropped, distances) where kept/dropped are lists of
    (path, size) and distances maps a dropped path to
    (nearest_kept_path, distance).
    """
    import numpy as np

    kept, dropped, distances = [], [], {}
    kept_sigs = []

    for path in paths:
        try:
            size, sig = read_size_and_signature(path)
        except OSError as e:
            logger.warning("skipping unreadable %s: %s", path.name, e)
            continue

        if not kept_sigs:
            kept.append((path, size))
            kept_sigs.append(sig)
            distances[path] = (None, float("inf"))
            continue

        # Mean absolute difference against every kept signature.
        stack = np.stack(kept_sigs)
        diffs = np.abs(stack - sig).mean(axis=1)
        nearest = int(diffs.argmin())
        best = float(diffs[nearest])

        if best <= threshold:
            dropped.append((path, size))
            distances[path] = (kept[nearest][0], best)
        else:
            kept.append((path, size))
            kept_sigs.append(sig)
            distances[path] = (kept[nearest][0], best)

    return kept, dropped, distances


def report_resolutions(entries, label):
    """Print the resolution/aspect spread and warn when it is mixed."""
    counts = Counter(size for _, size in entries)
    if not counts:
        return
    print(f"\n=== capture resolutions ({label}) ===")
    for (w, h), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        print(f"  {w}x{h:<6} aspect={w / h:5.2f}  frames={n}")
    if len(counts) > 1:
        print(
            f"\n  WARNING: {len(counts)} different resolutions present.\n"
            "  A detector dataset should be shot at ONE resolution and aspect\n"
            "  ratio -- the same one production will run. Mixed aspect ratios\n"
            "  produce mixed letterbox padding, which is variation that hurts\n"
            "  rather than helps. Pick one and re-shoot the rest."
        )


def report_distances(paths, distances, threshold):
    """Histogram of nearest-kept distances, to help tune --threshold."""
    finite = [d for _, d in (distances.get(p, (None, float("inf")))
                             for p in paths) if d != float("inf")]
    if not finite:
        return
    print("\n=== nearest-neighbour distance distribution ===")
    edges = [0, 0.5, 1, 2, 4, 8, 16, 32, float("inf")]
    for lo, hi in zip(edges, edges[1:]):
        n = sum(1 for d in finite if lo <= d < hi)
        if not n:
            continue
        hi_s = "inf" if hi == float("inf") else f"{hi:g}"
        mark = "  <- dropped" if hi <= threshold else ""
        print(f"  {lo:>5g} .. {hi_s:<5} {'#' * min(n, 50)} {n}{mark}")
    print(f"\n  threshold = {threshold:g} (mean abs diff, 0-255 scale)")


def main():
    global BACKEND

    ap = argparse.ArgumentParser(
        description="Drop near-duplicate capture frames before labeling.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("--images-dir", required=True,
                    help="Directory of captured frames")
    ap.add_argument("--out", default=None,
                    help="Directory to write the deduped set into")
    ap.add_argument("--threshold", type=float, default=DEFAULT_THRESHOLD,
                    help=f"Mean-abs-diff cutoff (default {DEFAULT_THRESHOLD})")
    ap.add_argument("--exclude", nargs="*", default=["overlay"],
                    help="Skip filenames containing these substrings "
                         "(default: overlay, which skips DDA's *.overlay.jpg)")
    ap.add_argument("--link", action="store_true",
                    help="Symlink into --out instead of copying")
    ap.add_argument("--report-only", action="store_true",
                    help="Analyze and report; write nothing")
    args = ap.parse_args()

    images_dir = Path(args.images_dir)
    if not images_dir.is_dir():
        logger.error("not a directory: %s", images_dir)
        sys.exit(1)
    if not args.out and not args.report_only:
        logger.error("give --out DIR, or --report-only")
        sys.exit(1)

    BACKEND = _load_backend()
    try:
        import numpy  # noqa: F401
    except ImportError:
        logger.error("Needs numpy:  pip install numpy")
        sys.exit(2)

    paths = find_images(images_dir, args.exclude)
    if not paths:
        logger.error("no images found under %s", images_dir)
        sys.exit(1)
    logger.info("found %d frames (backend=%s)", len(paths), BACKEND)

    kept, dropped, distances = dedupe(paths, args.threshold)

    print(f"\n=== dedupe: kept {len(kept)} of {len(paths)} "
          f"({len(dropped)} near-duplicates dropped) ===")
    if dropped:
        print("\ndropped frames (nearest kept frame, distance):")
        for path, _ in dropped:
            near, dist = distances[path]
            near_name = near.name if near is not None else "-"
            print(f"  {path.name:<48} ~ {near_name:<48} {dist:.2f}")

    report_distances(paths, distances, args.threshold)
    report_resolutions(kept, "kept frames")

    if len(kept) < 100:
        print(
            f"\n  NOTE: {len(kept)} distinct frames is below the ~150 needed for\n"
            "  a first detector fine-tune (and ~300 for a production set).\n"
            "  Keep capturing, and move something in the scene between frames."
        )

    if args.report_only:
        return

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    for path, _ in kept:
        dest = out_dir / path.name
        if dest.exists() or dest.is_symlink():
            dest.unlink()
        if args.link:
            dest.symlink_to(path.resolve())
        else:
            shutil.copy2(path, dest)
    action = "symlinked" if args.link else "copied"
    print(f"\n{action} {len(kept)} frames -> {out_dir}")


if __name__ == "__main__":
    main()
