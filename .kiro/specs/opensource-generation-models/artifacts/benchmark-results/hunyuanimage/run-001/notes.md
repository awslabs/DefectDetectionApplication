# HunyuanImage — run-001 notes (⚠ benchmarked as HunyuanImage-2.1, substitution)

Run: `hunyuan21-r1` · g6e.8xlarge (L40S 48 GB, 248 GiB RAM) · 2026-08-17 · protocol §3/§5
Weights: `hunyuanvideo-community/HunyuanImage-2.1-Diffusers` (official diffusers
conversion of `tencent/HunyuanImage-2.1`, 17B), `HunyuanImagePipeline` (diffusers 0.39).
Memory strategy: **bitsandbytes NF4 on transformer + text_encoder** + `enable_model_cpu_offload`.

## Substitution record (Req 1.7 / matrix "Sizing note for Task 4.3")

The Evaluation_Matrix row is pinned to **HunyuanImage-3.0** (80B MoE, 13B active
per token), whose bf16 footprint needs 3–4×80 GB — p4de.24xlarge / p5.48xlarge
class, far outside the USD 500 Cost_Cap for a single exploration run. Per the
matrix's sanctioned option (c), this run benchmarks **HunyuanImage-2.1 (17B)**
instead. Consequences for downstream phases:

- Latency, load time, and cost figures below describe **2.1, not 3.0**, and must
  not be attributed to the matrix's HunyuanImage-3.0 row.
- 3.0's instruction-based I2I editing (HunyuanImage-3.0-Instruct) was **not**
  measured. 2.1 has no image conditioning at all, so the inpainting result below
  is a property of the substitute, and the matrix's separate finding stands for
  3.0: **no documented binary source+mask API** for either version.
- The substitution is also recorded in the run index of
  `../../README.md` and in `config.json`.

## Summary

- **4/13 cases ok** — all 4 text-to-image cases succeeded; **all 9 inpainting
  cases recorded `failed / failure_mode: unsupported_task`** (protocol §3 step 5).
- **model_load_seconds: 18.92** (warm HF cache; weights download 62.9 s measured separately).
- **Per-image latency, t2i (50 steps, 1024×1024): 39.5–40.0 s** (first successful
  case 39.73 s, steady state ≈ 39.7 s).
- **Cold_Start_Time proxy** (`model_load_seconds` + first *successful* case
  latency): 18.92 + 39.73 ≈ **58.7 s** on a warm cache; add ≈63 s for the
  weights download on a truly cold instance. (The literal first case in manifest
  order is `inpaint-001`, which fails in microseconds on the unsupported-task
  check and is not a meaningful latency data point.)
- Full outputs: `s3://opensource-genmodels-benchmark-164152369890/runs/hunyuan21-r1/`
  (until Phase D teardown); representative copies in `outputs/` (t2i-001, t2i-004).

## Inpainting: unsupported (expected per the matrix)

`HunyuanImagePipeline.__call__` takes no `image` / `mask_image` argument, and
diffusers ships no Hunyuan inpaint pipeline. The 9 inpainting cases were
therefore attempted and recorded as
`failed / unsupported_task: HunyuanImagePipeline is text-to-image only (no
mask/image conditioning; no Hunyuan inpaint pipeline in diffusers)` — the case
loop continued through every remaining case (Req 2.10, visible in
`metrics.json`).

**No rubric scores are recorded**: the protocol's §5 rubric applies to
inpainting outputs, and none exist. HunyuanImage cannot serve the pipeline's
primary (mask-based defect insertion) path in either version — 2.1 has no image
conditioning, 3.0 has instruction editing with no documented mask API.

## Failure notes

- **Attempt 1 — bf16 + `enable_model_cpu_offload`** (the flux1-r1 pattern):
  **CUDA OOM on all 4 t2i cases** (`attempt-1-bf16-offload-metrics.json`:
  t2i-001 fails after 7.70 s, "Tried to allocate 124.00 MiB … 69 MiB free of
  44.39 GiB"; the following three fail immediately on the exhausted allocator).
  The 17B DiT is ~34 GB bf16 and the guidance path (two conditions per step)
  pushes activations past the remaining ~10 GB on a 44.4 GiB-usable L40S. The
  case loop recorded all four and continued — failure isolation held.
- **Attempt 2 — NF4 quantization** succeeded for all 4 t2i cases.
- Per-case failure isolation worked as designed in both attempts: no run aborted.

## Observations for the hosting comparison / cost model

- ~40 s per 1024² image at 50 steps under NF4+offload. Per-step cost is roughly
  half of FLUX.2 [dev]'s, but the model is text-to-image only — it is a
  **T2I-only data point**, in the same functional class as PixArt for this
  pipeline, at large-class instance cost.
- 17B bf16 does **not** fit a single 48 GB L40S with cpu_offload; a resident
  bf16 deployment needs ≥64–80 GB VRAM, or 4-bit quantization to stay on L40S.
- Licensing (from the matrix, unchanged by this run): Tencent Hunyuan Community
  License — EU/UK/South Korea territory exclusion, >100M MAU separate-license
  threshold, and for 2.1 specifically **§5(b) prohibits using outputs to improve
  other AI models**, which is exactly this pipeline's purpose (synthetic
  training data). Legal_Review_Flag stands; likely `unsuitable` for Phase G
  independent of quality.
