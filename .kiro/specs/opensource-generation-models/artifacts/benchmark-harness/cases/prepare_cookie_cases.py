#!/usr/bin/env python3
"""Deterministic preparation of the real-imagery (cookie) benchmark cases.

Source images are representative production images copied READ-ONLY from the
portal account's training data:

    aws s3 cp s3://ryvan-cookies/training-images/normal-1.jpg  <src>/
    aws s3 cp s3://ryvan-cookies/training-images/normal-10.jpg <src>/
    aws s3 cp s3://ryvan-cookies/training-images/normal-17.jpg <src>/

Processing is fully deterministic (center-crop 576x576 → LANCZOS upscale to
768x768, PNG without optimization; masks drawn with fixed coordinates), so
re-running this script over the same source JPEGs reproduces identical files.

The committed PNGs are the frozen inputs (protocol §2); this script documents
their provenance. Do NOT regenerate after the first Benchmark_Run without
following the protocol's immutability rule (§2.3).

Usage: python3 prepare_cookie_cases.py --src /tmp/cookie-src [--out DIR]
"""

import argparse
import json
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 768

# (case_id, source jpg, mask shape, defect type, prompt, seed)
COOKIE_CASES = [
    ("inpaint-101", "normal-1.jpg", "broken-edge",
     "a chocolate chip cookie with a large broken-off chunk missing from its edge, "
     "top-down food inspection photo", 111),
    ("inpaint-102", "normal-10.jpg", "burn-patch",
     "a dark burned patch on the surface of a chocolate chip cookie, "
     "overhead quality-control photo", 112),
    ("inpaint-103", "normal-17.jpg", "crack-thin",
     "a deep crack running across the surface of a chocolate chip cookie, "
     "macro food defect photo", 113),
]

COOKIE_T2I = [
    ("t2i-004",
     "a broken chocolate chip cookie split into pieces on a conveyor belt, "
     "top-down food factory inspection photo", 204),
]


def _prepare_source(src_path: Path) -> Image.Image:
    """576x768 JPEG → deterministic 768x768 PNG (center-crop then upscale)."""
    im = Image.open(src_path).convert("RGB")
    w, h = im.size
    side = min(w, h)
    left = (w - side) // 2
    top = (h - side) // 2
    im = im.crop((left, top, left + side, top + side))
    return im.resize((SIZE, SIZE), Image.LANCZOS)


def _mask(shape: str) -> Image.Image:
    """Binary mask (white = inpaint region) with a deterministic shape.

    Shapes vary in size and position across the cookie cases:
    broken-edge ~12% area at the cookie rim, burn-patch ~7% off-center,
    crack-thin ~2.5% across the middle.
    """
    m = Image.new("L", (SIZE, SIZE), 0)
    d = ImageDraw.Draw(m)
    if shape == "broken-edge":       # large wedge at the right rim, ~12% area
        d.polygon([(560, 190), (760, 130), (768, 480), (600, 470), (540, 330)],
                  fill=255)
    elif shape == "burn-patch":      # medium ellipse, upper-left of center, ~7% area
        d.ellipse([190, 170, 470, 380], fill=255)
    else:  # crack-thin              # long thin diagonal, ~2.5% area
        d.polygon([(150, 500), (170, 485), (620, 250), (635, 270)], fill=255)
        d.polygon([(390, 380), (405, 370), (500, 440), (488, 452)], fill=255)
    return m


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", required=True,
                    help="directory holding the normal-*.jpg source copies")
    ap.add_argument("--out", default=str(Path(__file__).parent))
    args = ap.parse_args()
    src = Path(args.src)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    new_entries = []
    for case_id, jpg, mask_shape, prompt, seed in COOKIE_CASES:
        img = _prepare_source(src / jpg)
        msk = _mask(mask_shape)
        img_name = f"{case_id}-source.png"
        msk_name = f"{case_id}-mask.png"
        img.save(out / img_name, format="PNG", optimize=False)
        msk.save(out / msk_name, format="PNG", optimize=False)
        new_entries.append({
            "case_id": case_id,
            "task_type": "inpainting",
            "prompt": prompt,
            "seed": seed,
            "image": img_name,
            "mask": msk_name,
        })

    for case_id, prompt, seed in COOKIE_T2I:
        new_entries.append({
            "case_id": case_id,
            "task_type": "text_to_image",
            "prompt": prompt,
            "seed": seed,
            "image": None,
            "mask": None,
        })

    manifest_path = out / "cases.json"
    manifest = json.loads(manifest_path.read_text()) if manifest_path.exists() else []
    existing_ids = {c["case_id"] for c in manifest}
    added = [e for e in new_entries if e["case_id"] not in existing_ids]
    # keep inpainting cases grouped before t2i cases, each group id-sorted
    manifest = sorted(
        manifest + added,
        key=lambda c: (0 if c["task_type"] == "inpainting" else 1, c["case_id"]),
    )
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print(f"Added {len(added)} cookie cases; manifest now has {len(manifest)} cases")


if __name__ == "__main__":
    main()
