#!/usr/bin/env python3
"""Objective proxies supporting rubric scoring (protocol §5 note: automated
metrics may be captured where cheap; they do not override rubric scores).

Per inpainting case: outside-mask MAE (background preservation proxy — lower
is better) and inside-mask MAE (did the model actually change the masked
region — near-zero means the case was a no-op).

Usage: python3 score_inpaint_proxy.py <cases_dir> <outputs_dir>
"""
import json
import sys
from pathlib import Path

import numpy as np
from PIL import Image


def main() -> None:
    cases_dir = Path(sys.argv[1])
    out_dir = Path(sys.argv[2])
    manifest = json.load(open(cases_dir / "cases.json"))
    print(f"{'case':<13} {'out_MAE':>8} {'in_MAE':>8} {'in/out':>7}")
    for c in manifest:
        if c["task_type"] != "inpainting":
            continue
        src = np.asarray(Image.open(cases_dir / c["image"]).convert("RGB"), dtype=np.float32)
        mask = np.asarray(Image.open(cases_dir / c["mask"]).convert("L"), dtype=np.float32) / 255.0
        gen_p = out_dir / f"{c['case_id']}.png"
        if not gen_p.exists():
            print(f"{c['case_id']:<13} MISSING")
            continue
        gen = Image.open(gen_p).convert("RGB")
        if gen.size != (src.shape[1], src.shape[0]):
            gen = gen.resize((src.shape[1], src.shape[0]))
        gen = np.asarray(gen, dtype=np.float32)
        diff = np.abs(gen - src).mean(axis=2)
        m = mask > 0.5
        out_mae = diff[~m].mean()
        in_mae = diff[m].mean()
        print(f"{c['case_id']:<13} {out_mae:8.2f} {in_mae:8.2f} {in_mae / max(out_mae, 0.01):7.1f}")


if __name__ == "__main__":
    main()
