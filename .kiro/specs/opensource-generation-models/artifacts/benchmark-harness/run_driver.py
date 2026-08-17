#!/usr/bin/env python3
"""diffusers-based run driver — executes one Benchmark_Run on the GPU instance.

This module is import-guarded: torch/diffusers are only imported inside
functions, so the pure harness logic (cost_cap, ledger, runner) stays testable
on machines without GPU dependencies. Run this script ON the benchmark
instance (protocol §3, steps 4–6).

Pipelines are loaded **sequentially per task group** (one pipeline resident on
the GPU at a time): medium-class models (FLUX.1 family, ~12B + T5-XXL ≈ 34 GB
bf16 per pipeline) cannot co-reside two pipelines on a 48 GB L40S. Case
results are re-assembled in frozen-manifest order before metrics.json is
written, so metrics are comparable across models regardless of load order.

Usage (on the instance):
    python3 run_driver.py --model flux.1-dev --run-id flux1dev-r1 \
        --instance-type g6e.xlarge --cases-dir cases/ \
        --output-bucket s3://<benchmark-bucket>/runs/flux1dev-r1/outputs --out metrics.json
"""

import argparse
import gc
import json
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from runner import (
    build_run_record,
    load_cases,
    run_cases,
    write_metrics,
)

# Candidate → task → (diffusers pipeline class name, HF repo, call kwargs).
# Pipeline classes are resolved lazily inside _load_pipeline().
# Call kwargs pin inference-time parameters for reproducibility (e.g. schnell
# is timestep-distilled: 4 steps, guidance ignored; Fill-dev's recommended
# guidance_scale is 30).
MODEL_SPECS: Dict[str, Dict[str, Optional[Tuple]]] = {
    # FLUX.1 family entries carry a 4th tuple element {"offload": True}: a
    # full .to("cuda") of a FLUX pipeline OOMs a 48 GB L40S at load (observed
    # on flux1-r1), so the FLUX pipes run bf16 with enable_model_cpu_offload.
    "flux.1-dev": {
        "t2i": ("FluxPipeline", "black-forest-labs/FLUX.1-dev",
                {"num_inference_steps": 28, "guidance_scale": 3.5},
                {"offload": True}),
        # Official inpainting variant per the Evaluation_Matrix — measured as
        # its own run (model key flux.1-fill-dev) so per-model metrics stay
        # separable; kept here for a combined run if ever requested.
        "inpaint": ("FluxFillPipeline", "black-forest-labs/FLUX.1-Fill-dev",
                    {"num_inference_steps": 50, "guidance_scale": 30.0},
                    {"offload": True}),
    },
    "flux.1-fill-dev": {
        # Dedicated run for the official FLUX.1-dev inpainting variant
        # (requires diffusers >= 0.32 for FluxFillPipeline).
        "t2i": None,
        "inpaint": ("FluxFillPipeline", "black-forest-labs/FLUX.1-Fill-dev",
                    {"num_inference_steps": 50, "guidance_scale": 30.0},
                    {"offload": True}),
    },
    "flux.1-schnell": {
        "t2i": ("FluxPipeline", "black-forest-labs/FLUX.1-schnell",
                {"num_inference_steps": 4, "guidance_scale": 0.0,
                 "max_sequence_length": 256},
                {"offload": True}),
        # No official fill variant — community path via the generic
        # FluxInpaintPipeline (strength-based, timestep-distilled 4 steps).
        # Attempted per the Evaluation_Matrix; failures are recorded per case.
        "inpaint": ("FluxInpaintPipeline", "black-forest-labs/FLUX.1-schnell",
                    {"num_inference_steps": 4, "guidance_scale": 0.0,
                     "strength": 0.85, "max_sequence_length": 256},
                    {"offload": True}),
    },
    "flux.2": {
        # Pinned after the Evaluation_Matrix verifies FLUX.2 repo/pipeline names.
        "t2i": ("FluxPipeline", "TBD-per-evaluation-matrix", {}),
        "inpaint": None,
    },
    "hunyuanimage": {
        # HunyuanDiT / HunyuanImage pipeline pinned after matrix verification.
        "t2i": ("HunyuanDiTPipeline", "Tencent-Hunyuan/HunyuanDiT-v1.2-Diffusers", {}),
        "inpaint": None,
    },
    "pixart-alpha": {
        "t2i": ("PixArtAlphaPipeline", "PixArt-alpha/PixArt-XL-2-1024-MS", {}),
        "inpaint": None,
    },
    "pixart-sigma": {
        "t2i": ("PixArtSigmaPipeline", "PixArt-alpha/PixArt-Sigma-XL-2-1024-MS", {}),
        "inpaint": None,
    },
}

TASK_KEY = {"text_to_image": "t2i", "inpainting": "inpaint"}


def _load_pipeline(entry: Tuple) -> Tuple[Any, float]:
    """Load one diffusers pipeline to CUDA; returns (pipe, load_seconds).

    torch/diffusers imported here only — import-guarded by design.
    """
    import torch  # noqa: PLC0415
    import diffusers  # noqa: PLC0415

    cls_name, repo = entry[0], entry[1]
    opts = entry[3] if len(entry) > 3 else {}
    cls = getattr(diffusers, cls_name)
    start = time.monotonic()
    pipe = cls.from_pretrained(repo, torch_dtype=torch.bfloat16)
    if opts.get("offload"):
        pipe.enable_model_cpu_offload()
    else:
        pipe.to("cuda")
    load_seconds = time.monotonic() - start
    return pipe, load_seconds


def _free_pipeline(pipe: Any) -> None:
    """Release a pipeline's GPU memory before the next task group loads."""
    import torch  # noqa: PLC0415

    del pipe
    gc.collect()
    torch.cuda.empty_cache()


def _make_generate(pipe: Optional[Any], call_kwargs: Dict[str, Any],
                   cases_dir: Path, out_dir: Path, output_bucket: str):
    """Build the per-case generate callable injected into runner.run_cases."""
    import torch  # noqa: PLC0415
    from PIL import Image  # noqa: PLC0415

    def generate(case: Dict[str, Any]) -> str:
        if pipe is None:
            raise RuntimeError("unsupported_task: no pipeline for this task on this model")
        gen = torch.Generator(device="cuda").manual_seed(case["seed"])
        if case["task_type"] == "inpainting":
            image = Image.open(cases_dir / case["image"]).convert("RGB")
            mask = Image.open(cases_dir / case["mask"]).convert("L")
            result = pipe(prompt=case["prompt"], image=image, mask_image=mask,
                          height=image.height, width=image.width,
                          generator=gen, **call_kwargs).images[0]
        else:
            result = pipe(prompt=case["prompt"], generator=gen, **call_kwargs).images[0]
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
    ap.add_argument("--tasks", default=None,
                    help="comma-separated task_type filter (e.g. 'inpainting'); "
                         "used when a model's task groups are measured as "
                         "separate runs (flux.1-dev t2i vs flux.1-fill-dev)")
    args = ap.parse_args()

    cases_dir = Path(args.cases_dir)
    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)

    cases = load_cases(cases_dir / "cases.json")
    if args.tasks:
        wanted = {t.strip() for t in args.tasks.split(",") if t.strip()}
        cases = [c for c in cases if c["task_type"] in wanted]
    spec = MODEL_SPECS[args.model]

    # Sequential task-group execution: one pipeline on the GPU at a time.
    task_order = list(dict.fromkeys(c["task_type"] for c in cases))
    results_by_case: Dict[str, Dict[str, Any]] = {}
    load_seconds_by_task: Dict[str, float] = {}
    for task_type in task_order:
        subset = [c for c in cases if c["task_type"] == task_type]
        entry = spec.get(TASK_KEY[task_type])
        pipe, call_kwargs = None, {}
        if entry is not None:
            pipe, secs = _load_pipeline(entry)
            call_kwargs = entry[2] if len(entry) > 2 else {}
            load_seconds_by_task[task_type] = secs
        generate = _make_generate(pipe, call_kwargs, cases_dir, out_dir,
                                  args.output_bucket)
        for result in run_cases(subset, generate):
            results_by_case[result["case_id"]] = result
        # Drop the closure's reference before freeing, otherwise the previous
        # pipeline stays resident and the next task-group load OOMs.
        generate = None
        if pipe is not None:
            _free_pipeline(pipe)
            pipe = None

    # Re-assemble in frozen-manifest order (protocol §3 step 5).
    results = [results_by_case[c["case_id"]] for c in cases]

    record = build_run_record(
        run_id=args.run_id,
        model=args.model,
        instance_type=args.instance_type,
        model_load_seconds=sum(load_seconds_by_task.values()),
        case_results=results,
        instance_hours=args.instance_hours,
        estimated_cost_usd=args.instance_hours * args.hourly_rate_usd,
    )
    write_metrics(record, Path(args.out))
    print(json.dumps({"run_id": args.run_id,
                      "ok": sum(1 for r in results if r["status"] == "ok"),
                      "failed": sum(1 for r in results if r["status"] == "failed"),
                      "model_load_seconds": round(sum(load_seconds_by_task.values()), 2),
                      "load_seconds_by_task": {k: round(v, 2) for k, v in
                                               load_seconds_by_task.items()}}))


if __name__ == "__main__":
    main()
