#!/usr/bin/env python3
"""Deterministic generator for the frozen benchmark test-case inputs.

Produces synthetic industrial-inspection-style source images and binary mask
PNGs (768x768) plus the cases.json manifest. All randomness is seeded so the
outputs are byte-stable: re-running this script must reproduce identical files.

The committed PNGs under this directory are the frozen inputs (protocol §2);
this script documents their provenance. Do NOT regenerate after the first
Benchmark_Run without following the protocol's immutability rule (§2.3).

Usage: python3 generate_cases.py [--out DIR]
"""

import argparse
import json
import random
from pathlib import Path

from PIL import Image, ImageDraw

SIZE = 768
GEN_SEED = 42  # seed for image synthesis only; per-case generation seeds live in the manifest


def _texture(kind: str, seed: int) -> Image.Image:
    """Deterministic industrial-looking grayscale texture."""
    rng = random.Random(seed)
    img = Image.new("RGB", (SIZE, SIZE))
    px = img.load()
    if kind == "brushed":
        # horizontal brushed-metal gradient with per-row noise
        rows = [140 + rng.randint(-18, 18) for _ in range(SIZE)]
        for y in range(SIZE):
            base = rows[y]
            for x in range(SIZE):
                v = base + int(10 * ((x / SIZE) - 0.5))
                px[x, y] = (v, v, v + 4)
    elif kind == "plate":
        # diagonal gradient sheet with sparse speckle
        for y in range(SIZE):
            for x in range(SIZE):
                v = 110 + int(60 * (x + y) / (2 * SIZE))
                px[x, y] = (v, v + 2, v + 5)
        d = ImageDraw.Draw(img)
        for _ in range(400):
            sx, sy = rng.randint(0, SIZE - 1), rng.randint(0, SIZE - 1)
            c = rng.randint(90, 200)
            d.point((sx, sy), fill=(c, c, c))
    elif kind == "panel":
        # radial-ish stamped panel: concentric rectangles
        d = ImageDraw.Draw(img)
        img.paste((150, 152, 155), (0, 0, SIZE, SIZE))
        for i, inset in enumerate(range(40, SIZE // 2, 60)):
            c = 150 - i * 6
            d.rectangle([inset, inset, SIZE - inset, SIZE - inset],
                        outline=(c, c, c), width=8)
    elif kind == "seam":
        # vertical gradient with a horizontal weld seam band
        for y in range(SIZE):
            v = 120 + int(40 * y / SIZE)
            for x in range(SIZE):
                px[x, y] = (v, v, v)
        d = ImageDraw.Draw(img)
        cy = SIZE // 2
        d.rectangle([0, cy - 24, SIZE, cy + 24], fill=(170, 168, 160))
        for x in range(0, SIZE, 16):
            d.ellipse([x, cy - 20, x + 24, cy + 20], outline=(140, 138, 130), width=3)
    elif kind == "coated":
        # smooth coated surface, subtle vertical bands
        for y in range(SIZE):
            for x in range(SIZE):
                v = 96 + int(20 * (0.5 + 0.5 * ((x // 96) % 2)))
                px[x, y] = (v - 20, v, v + 30)
    else:  # cast
        # mottled cast-housing texture
        img.paste((105, 105, 108), (0, 0, SIZE, SIZE))
        d = ImageDraw.Draw(img)
        for _ in range(900):
            sx, sy = rng.randint(0, SIZE - 1), rng.randint(0, SIZE - 1)
            r = rng.randint(2, 6)
            c = 105 + rng.randint(-25, 25)
            d.ellipse([sx, sy, sx + r, sy + r], fill=(c, c, c + 2))
    return img


def _mask(shape: str) -> Image.Image:
    """Binary mask (white = inpaint region) with a deterministic shape."""
    m = Image.new("L", (SIZE, SIZE), 0)
    d = ImageDraw.Draw(m)
    if shape == "thin-diagonal":          # ~2% area
        d.polygon([(180, 160), (210, 150), (620, 560), (590, 575)], fill=255)
    elif shape == "ellipse-medium":       # ~8% area
        d.ellipse([230, 260, 560, 460], fill=255)
    elif shape == "circle-small":         # ~4% area
        d.ellipse([300, 300, 470, 470], fill=255)
    elif shape == "band-horizontal":      # ~6% area
        d.rectangle([80, 350, 690, 410], fill=255)
    elif shape == "rect-large":           # ~15% area
        d.rectangle([160, 200, 560, 430], fill=255)
        d.rectangle([360, 430, 560, 520], fill=255)
    else:  # crack-thin                    # ~3% area
        d.polygon([(120, 600), (140, 590), (640, 210), (655, 230)], fill=255)
        d.polygon([(380, 400), (395, 390), (520, 470), (508, 484)], fill=255)
    return m


CASES = [
    # (case_id, texture, mask shape, prompt, seed)
    ("inpaint-001", "brushed", "thin-diagonal",
     "a deep scratch on a brushed metal bracket, industrial inspection photo", 101),
    ("inpaint-002", "plate", "ellipse-medium",
     "a patch of orange-brown corrosion on a steel sheet, factory quality-control photo", 102),
    ("inpaint-003", "panel", "circle-small",
     "a shallow dent on a stamped metal panel, raking light inspection photo", 103),
    ("inpaint-004", "seam", "band-horizontal",
     "weld porosity with small gas holes along a weld seam, macro industrial photo", 104),
    ("inpaint-005", "coated", "rect-large",
     "a large paint chip exposing bare metal on a coated surface, inspection photo", 105),
    ("inpaint-006", "cast", "crack-thin",
     "a hairline crack in a cast aluminum housing, macro defect photo", 106),
]

T2I_CASES = [
    ("t2i-001", "a scratched metal plate, top-down industrial inspection photo, harsh lighting", 201),
    ("t2i-002", "a corroded steel surface with rust patches, factory quality-control photo", 202),
    ("t2i-003", "a cracked plastic housing, macro defect photography, shallow depth of field", 203),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).parent))
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    manifest = []
    for i, (case_id, texture, mask_shape, prompt, seed) in enumerate(CASES):
        img = _texture(texture, GEN_SEED + i)
        msk = _mask(mask_shape)
        img_name = f"{case_id}-source.png"
        msk_name = f"{case_id}-mask.png"
        img.save(out / img_name, format="PNG", optimize=False)
        msk.save(out / msk_name, format="PNG", optimize=False)
        manifest.append({
            "case_id": case_id,
            "task_type": "inpainting",
            "prompt": prompt,
            "seed": seed,
            "image": img_name,
            "mask": msk_name,
        })

    for case_id, prompt, seed in T2I_CASES:
        manifest.append({
            "case_id": case_id,
            "task_type": "text_to_image",
            "prompt": prompt,
            "seed": seed,
            "image": None,
            "mask": None,
        })

    with open(out / "cases.json", "w") as f:
        json.dump(manifest, f, indent=2)
        f.write("\n")
    print(f"Wrote {len(manifest)} cases to {out}")


if __name__ == "__main__":
    main()
