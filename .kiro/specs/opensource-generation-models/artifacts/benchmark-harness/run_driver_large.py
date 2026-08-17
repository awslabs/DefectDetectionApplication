#!/usr/bin/env python3
"""Large-class run driver (task 4.3): FLUX.2 [dev] and HunyuanImage.

Separate from `run_driver.py` because the large-class models need memory
strategies and call signatures the medium/small driver does not model:

* **FLUX.2 [dev]** (`Flux2Pipeline`, diffusers >= 0.36) — 32B DiT + a
  Mistral-3 24B-class VLM text encoder ≈ 112 GB bf16. A module-level
  `enable_model_cpu_offload()` cannot help because the transformer alone
  (~64 GB bf16) exceeds a 48 GB L40S, so the driver supports
  `--strategy bnb4` (bitsandbytes NF4 on transformer + text_encoder, the
  officially documented diffusers quantization path) alongside
  `--strategy offload` for the empirical OOM record.
  `Flux2Pipeline.__call__` takes reference `image`s for editing and has **no
  `mask_image` parameter** — diffusers ships a mask-based inpaint pipeline only
  for FLUX.2 [klein] (`Flux2KleinInpaintPipeline`, Qwen3 text encoder, not
  loadable with [dev] weights). Inpainting cases therefore run through the
  official *instruction editing* path with the frozen source as the single
  reference image, and mask parity is assessed post-hoc against the frozen
  mask (protocol §5 + notes.md).

* **HunyuanImage** (`HunyuanImagePipeline`) — the matrix-sanctioned substitute
  HunyuanImage-2.1 (17B, ~34 GB bf16 DiT) fits the flux1-r1 pattern:
  bf16 + `enable_model_cpu_offload()` on a 48 GB GPU with ≥64 GiB host RAM.
  The pipeline is text-to-image only (no `image` / `mask_image` parameters and
  no Hunyuan inpaint pipeline in diffusers), so inpainting cases are recorded
  `failed / unsupported_task` per protocol §3 step 5.

Run ON the benchmark instance (protocol §3, steps 4–6):

    python3 run_driver_large.py --model flux.2 --run-id flux2-r1 \
        --instance-type g6e.8xlarge --cases-dir cases/ --strategy bnb4 \
        --output-bucket s3://<bucket>/runs/flux2-r1/outputs --out metrics.json
"""

import argparse
import gc
import json
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from runner import build_run_record, load_cases, run_cases, write_metrics

FLUX2_REPO = "black-forest-labs/FLUX.2-dev"
# Matrix-sanctioned substitution: HunyuanImage-3.0 is an 80B MoE needing
# p4de/p5-class hardware (outside the Cost_Cap); HunyuanImage-2.1 (17B) is the
# recorded substitute. Official diffusers conversion of tencent/HunyuanImage-2.1.
HUNYUAN_REPO = "hunyuanvideo-community/HunyuanImage-2.1-Diffusers"

# FLUX.2 [dev] has no mask input; the frozen prompt is wrapped in a fixed
# editing instruction so the reference-editing path has an actionable verb.
# The wrapper is constant across cases and recorded in config.json.
EDIT_PREFIX = "Edit this photo: add "
EDIT_SUFFIX = ". Change nothing else in the image."

UNSUPPORTED_HUNYUAN_INPAINT = (
    "unsupported_task: HunyuanImagePipeline is text-to-image only "
    "(no mask/image conditioning; no Hunyuan inpaint pipeline in diffusers)"
)


def _load_flux2(strategy: str, steps: int) -> Tuple[Any, float, Dict[str, Any]]:
    """Load FLUX.2 [dev]; returns (pipe, load_seconds, meta)."""
    import torch
    from diffusers import Flux2Pipeline

    meta: Dict[str, Any] = {"strategy": strategy}
    start = time.monotonic()
    if strategy == "bnb4":
        from diffusers import PipelineQuantizationConfig

        quant = PipelineQuantizationConfig(
            quant_backend="bitsandbytes_4bit",
            quant_kwargs={
                "load_in_4bit": True,
                "bnb_4bit_quant_type": "nf4",
                "bnb_4bit_compute_dtype": torch.bfloat16,
            },
            components_to_quantize=["transformer", "text_encoder"],
        )
        pipe = Flux2Pipeline.from_pretrained(
            FLUX2_REPO, quantization_config=quant, torch_dtype=torch.bfloat16
        )
        try:
            pipe.enable_model_cpu_offload()
            meta["placement"] = "enable_model_cpu_offload (nf4 transformer + text_encoder)"
        except Exception as exc:  # noqa: BLE001
            pipe.to("cuda")
            meta["placement"] = f"to(cuda) after offload failed: {type(exc).__name__}: {exc}"
    else:
        pipe = Flux2Pipeline.from_pretrained(FLUX2_REPO, torch_dtype=torch.bfloat16)
        pipe.enable_model_cpu_offload()
        meta["placement"] = "enable_model_cpu_offload (bf16, no quantization)"
    load_seconds = time.monotonic() - start
    meta["num_inference_steps"] = steps
    return pipe, load_seconds, meta


def _load_hunyuan(strategy: str, steps: int) -> Tuple[Any, float, Dict[str, Any]]:
    """Load HunyuanImage-2.1.

    `offload`: bf16 + `enable_model_cpu_offload()` — the flux1-r1 pattern.
    Measured on a 44.4 GiB-usable L40S this OOMs: the 17B DiT is ~34 GB bf16
    and the pipeline's guidance path (two conditions per step) pushes
    activations past the remaining ~10 GB.
    `bnb4`: bitsandbytes NF4 on transformer + text_encoder (same officially
    documented diffusers quantization path used for FLUX.2 [dev]).
    """
    import torch
    from diffusers import HunyuanImagePipeline

    start = time.monotonic()
    if strategy == "bnb4":
        from diffusers import PipelineQuantizationConfig

        quant = PipelineQuantizationConfig(
            quant_backend="bitsandbytes_4bit",
            quant_kwargs={
                "load_in_4bit": True,
                "bnb_4bit_quant_type": "nf4",
                "bnb_4bit_compute_dtype": torch.bfloat16,
            },
            components_to_quantize=["transformer", "text_encoder"],
        )
        pipe = HunyuanImagePipeline.from_pretrained(
            HUNYUAN_REPO, quantization_config=quant, torch_dtype=torch.bfloat16
        )
        placement = "enable_model_cpu_offload (nf4 transformer + text_encoder)"
    else:
        pipe = HunyuanImagePipeline.from_pretrained(HUNYUAN_REPO, torch_dtype=torch.bfloat16)
        placement = "enable_model_cpu_offload (bf16, no quantization)"
    pipe.enable_model_cpu_offload()
    load_seconds = time.monotonic() - start
    return pipe, load_seconds, {
        "strategy": strategy,
        "placement": placement,
        "num_inference_steps": steps,
    }


def _free(pipe: Any) -> None:
    import torch

    del pipe
    gc.collect()
    torch.cuda.empty_cache()


def _make_generate(model: str, pipe: Optional[Any], cases_dir: Path, out_dir: Path,
                   output_bucket: str, steps: int, t2i_size: int, edit_size: int,
                   guidance: float):
    import torch
    from PIL import Image

    def generate(case: Dict[str, Any]) -> str:
        gen = torch.Generator(device="cuda").manual_seed(case["seed"])
        if case["task_type"] == "inpainting":
            if model == "hunyuanimage" or pipe is None:
                raise RuntimeError(UNSUPPORTED_HUNYUAN_INPAINT)
            # FLUX.2 [dev] instruction-editing path (no mask parameter exists).
            source = Image.open(cases_dir / case["image"]).convert("RGB")
            prompt = f"{EDIT_PREFIX}{case['prompt']}{EDIT_SUFFIX}"
            result = pipe(
                image=[source], prompt=prompt,
                height=edit_size, width=edit_size,
                num_inference_steps=steps, guidance_scale=guidance,
                generator=gen,
            ).images[0]
        elif model == "hunyuanimage":
            result = pipe(
                prompt=case["prompt"], negative_prompt="",
                height=t2i_size, width=t2i_size,
                num_inference_steps=steps, generator=gen,
            ).images[0]
        else:
            result = pipe(
                prompt=case["prompt"],
                height=t2i_size, width=t2i_size,
                num_inference_steps=steps, guidance_scale=guidance,
                generator=gen,
            ).images[0]
        local = out_dir / f"{case['case_id']}.png"
        result.save(local)
        return f"{output_bucket.rstrip('/')}/{local.name}"

    return generate


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", required=True, choices=["flux.2", "hunyuanimage"])
    ap.add_argument("--run-id", required=True)
    ap.add_argument("--instance-type", required=True)
    ap.add_argument("--cases-dir", required=True)
    ap.add_argument("--output-bucket", required=True)
    ap.add_argument("--out", default="metrics.json")
    ap.add_argument("--strategy", default="bnb4", choices=["bnb4", "offload"],
                    help="memory strategy: bnb4 = bitsandbytes NF4 on "
                         "transformer + text_encoder; offload = bf16 with "
                         "enable_model_cpu_offload (OOMs on a 48 GB L40S for "
                         "both large-class models — measured, see notes.md)")
    ap.add_argument("--steps", type=int, default=28)
    ap.add_argument("--t2i-size", type=int, default=1024)
    ap.add_argument("--edit-size", type=int, default=768,
                    help="inpainting/editing output size (frozen sources are 768x768)")
    ap.add_argument("--guidance", type=float, default=4.0)
    ap.add_argument("--cases", default=None,
                    help="comma-separated case_id filter (smoke tests)")
    ap.add_argument("--tasks", default=None, help="comma-separated task_type filter")
    ap.add_argument("--instance-hours", type=float, default=0.0)
    ap.add_argument("--hourly-rate-usd", type=float, default=0.0)
    ap.add_argument("--meta-out", default=None,
                    help="write load/strategy metadata JSON here (feeds config.json)")
    args = ap.parse_args()

    cases_dir = Path(args.cases_dir)
    out_dir = Path("outputs")
    out_dir.mkdir(exist_ok=True)

    cases: List[Dict[str, Any]] = load_cases(cases_dir / "cases.json")
    if args.tasks:
        wanted = {t.strip() for t in args.tasks.split(",") if t.strip()}
        cases = [c for c in cases if c["task_type"] in wanted]
    if args.cases:
        wanted_ids = {c.strip() for c in args.cases.split(",") if c.strip()}
        cases = [c for c in cases if c["case_id"] in wanted_ids]

    needs_pipeline = any(
        c["task_type"] == "text_to_image" or args.model == "flux.2" for c in cases
    )
    pipe, load_seconds, meta = None, 0.0, {"strategy": "none"}
    if needs_pipeline:
        if args.model == "flux.2":
            pipe, load_seconds, meta = _load_flux2(args.strategy, args.steps)
        else:
            pipe, load_seconds, meta = _load_hunyuan(args.strategy, args.steps)

    generate = _make_generate(args.model, pipe, cases_dir, out_dir,
                              args.output_bucket, args.steps, args.t2i_size,
                              args.edit_size, args.guidance)
    results = run_cases(cases, generate)
    generate = None
    if pipe is not None:
        _free(pipe)
        pipe = None

    record = build_run_record(
        run_id=args.run_id, model=args.model, instance_type=args.instance_type,
        model_load_seconds=load_seconds, case_results=results,
        instance_hours=args.instance_hours,
        estimated_cost_usd=args.instance_hours * args.hourly_rate_usd,
    )
    write_metrics(record, Path(args.out))

    meta.update({
        "model_load_seconds": load_seconds,
        "repo": FLUX2_REPO if args.model == "flux.2" else HUNYUAN_REPO,
        "t2i_size": args.t2i_size, "edit_size": args.edit_size,
        "guidance_scale": args.guidance,
        "edit_prompt_wrapper": (EDIT_PREFIX + "<frozen prompt>" + EDIT_SUFFIX)
        if args.model == "flux.2" else None,
    })
    if args.meta_out:
        Path(args.meta_out).write_text(json.dumps(meta, indent=2) + "\n")
    print(json.dumps({
        "run_id": args.run_id,
        "ok": sum(1 for r in results if r["status"] == "ok"),
        "failed": sum(1 for r in results if r["status"] == "failed"),
        "model_load_seconds": round(load_seconds, 2),
        "meta": meta,
        "latencies": {r["case_id"]: round(r["latency_seconds"], 2) for r in results},
        "failures": {r["case_id"]: r["failure_mode"] for r in results
                     if r["status"] == "failed"},
    }, indent=2))


if __name__ == "__main__":
    main()
