#!/usr/bin/env python3
"""diffusers-based run driver — executes one Benchmark_Run on the GPU instance.

This module is import-guarded: torch/diffusers are only imported inside
functions, so the pure harness logic (cost_cap, ledger, runner) stays testable
on machines without GPU dependencies. Run this script ON the benchmark
instance (protocol §3, steps 4–6).

Usage (on the instance):
    python3 run_driver.py --model flux.1-dev --run-id flux1dev-r1 \
        --instance-type g6e.xlarge --cases-dir cases/ \
        --output-bucket s3://<benchmark-bucket> --out metrics.json
"""

import argparse
import json
import time
from pathlib import Path
from typing import Any, Dict

from runner import (
    build_run_record,
    load_cases,
    run_cases,
    write_metrics,
)

# Candidate → (diffusers pipeline class name, HF repo, task support).
# Pipeline classes are resolved lazily inside _load_pipelines().
MODEL_SPECS: Dict[str, Dict[str, Any]] = {
    "flux.1-dev": {
        "t2i": ("FluxPipeline", "black-forest-labs/FLUX.1-dev"),
        # Official inpainting variant per the Evaluation_Matrix:
        "inpaint": ("FluxFillPipeline", "black-forest-labs/FLUX.1-Fill-dev"),
    },
    "flux.1-schnell": {
        "t2i": ("FluxPipeline", "black-forest-labs/FLUX.1-schnell"),
        "inpaint": None,  # no official fill variant — matrix decides community/unsupported
    },
    "flux.2": {
        # Pinned after the Evaluation_Matrix verifies FLUX.2 repo/pipeline names.
        "t2i": ("FluxPipeline", "TBD-per-evaluation-matrix"),
        "inpaint": None,
    },
    "hunyuanimage": {
        # HunyuanDiT / HunyuanImage pipeline pinned after matrix verification.
        "t2i": ("HunyuanDiTPipeline", "Tencent-Hunyuan/HunyuanDiT-v1.2-Diffusers"),
        "inpaint": None,
    },
    "pixart-alpha": {
        "t2i": ("PixArtAlphaPipeline", "PixArt-alpha/PixArt-XL-2-1024-MS"),
        "inpaint": None,
    },
    "pixart-sigma": {
        "t2i": ("PixArtSigmaPipeline", "PixArt-alpha/PixArt-Sigma-XL-2-1024-MS"),
        "inpaint": None,
    },
}


def _load_pipelines(model: str):
    """Load the model's diffusers pipeline(s); returns (pipes, load_seconds).

    torch/diffusers imported here only — import-guarded by design.
    """
    import torch  # noqa: PLC0415
    import diffusers  # noqa: PLC0415

    spec = MODEL_SPECS[model]
    pipes: Dict[str, Any] = {}
    start = time.monotonic()
    for task in ("t2i", "inpaint"):
        entry = spec.get(task)
        if entry is None:
            continue
        cls_name, repo = entry
        cls = getattr(diffusers, cls_name)
        pipe = cls.from_pretrained(repo, torch_dtype=torch.bfloat16)
        pipe.to("cuda")
        pipes[task] = pipe
    load_seconds = time.monotonic() - start
    return pipes, load_seconds


def _make_generate(pipes, cases_dir: Path, out_dir: Path, output_bucket: str):
    """Build the per-case generate callable injected into runner.run_cases."""
    import torch  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    def generate(case: Dict[str, Any]) -> str:
        gen = torch.Generator(device="cuda").manual_seed(case["seed"])
        if case["task_type"] == "inpainting":
            pipe = pipes.get("inpaint")
            if pipe is None:
                raise RuntimeError("unsupported_task: no inpainting pipeline for this model")
            image = Image.open(cases_dir / case["image"]).convert("RGB")
            mask = Image.open(cases_dir / case["mask"]).convert("L")
            result = pipe(prompt=case["prompt"], image=image, mask_image=mask,
                          generator=gen).images[0]
        else:
            pipe = pipes["t2i"]
            result = pipe(prompt=case["prompt"], generator=gen).images[0]
        local = out_dir / f"{case['case_id']}.png"
        result.save(local)
        uri = f"{output_bucket.rstrip('/')}/{local.name}"
        # Upload is done in bulk post-run via aws s3 sync (see user_data template);
        # the URI recorded here is where the object will land.
        return uri

    return generate


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=sorted(MODEL_SPECS))
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--instance-type", required=True)
    ap.add_argument("--cases-dir", required=True)
    ap.add_argument("--output-bucket", required=True)
    ap.add_argument("--out", default="metrics.json")
    ap.add_argument("--instance-hours", type=float, default=0.0,
                    help="filled post-terminate; may be patched into metrics.json afterwards")
    ap.add_argument("--hourly-rate-usd", type=float, default=0.0)
    args = ap.parse_args()

    cases_dir = Path(args.cases_dir)
    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)

    cases = load_cases(cases_dir / "cases.json")
    pipes, load_seconds = _load_pipelines(args.model)
    generate = _make_generate(pipes, cases_dir, out_dir, args.output_bucket)
    results = run_cases(cases, generate)

    record = build_run_record(
        run_id=args.run_id,
        model=args.model,
        instance_type=args.instance_type,
        model_load_seconds=load_seconds,
        case_results=results,
        instance_hours=args.instance_hours,
        estimated_cost_usd=args.instance_hours * args.hourly_rate_usd,
    )
    write_metrics(record, Path(args.out))
    print(json.dumps({"run_id": args.run_id,
                      "ok": sum(1 for r in results if r["status"] == "ok"),
                      "failed": sum(1 for r in results if r["status"] == "failed"),
                      "model_load_seconds": round(load_seconds, 2)}))


if __name__ == "__main__":
    main()
