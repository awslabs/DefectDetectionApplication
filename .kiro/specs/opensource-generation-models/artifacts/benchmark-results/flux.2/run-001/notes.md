# FLUX.2 [dev] — run-001 notes

Run: `flux2-r1` · g6e.8xlarge (L40S 48 GB, 248 GiB RAM) · 2026-08-17 · protocol §3/§5
Weights: `black-forest-labs/FLUX.2-dev` (32B DiT + Mistral-3 24B-class VLM encoder), HF-gated.
Memory strategy: **bitsandbytes NF4 on transformer + text_encoder** (diffusers
`PipelineQuantizationConfig`) + `enable_model_cpu_offload`.

## Summary

- **13/13 cases ok** (9 "inpainting" via the editing path + 4 t2i), zero hard failures.
- **model_load_seconds: 38.11** (warm HF cache; weights download 123.8 s measured separately).
- **Per-image latency, editing path (28 steps, 768×768): 80.2–84.7 s** (first case 84.66 s, steady state ≈ 81.5 s).
- **Per-image latency, t2i (28 steps, 1024×1024): 75.1–75.3 s.**
- **Cold_Start_Time proxy** (`model_load_seconds` + first-case latency): 38.11 + 84.66 ≈ **123 s** on a warm weights cache; add ≈124 s for the weights download on a truly cold instance.
- **Mask-based inpainting is not available for FLUX.2 [dev]** — see the mask-parity finding below. This is the headline result for the Phase G decision.
- Full outputs: `s3://opensource-genmodels-benchmark-164152369890/runs/flux2-r1/` (until Phase D teardown); representative copies in `outputs/` (inpaint-001, inpaint-005, inpaint-102, t2i-001, t2i-004).

## Mask parity finding (make-or-break for the primary path)

The Evaluation_Matrix recorded FLUX.2 [dev]'s inpainting path as `native`
("native image editing; mask-driven path to validate in Phase C"). Phase C
resolves that caveat **negatively**:

- `Flux2Pipeline.__call__` (diffusers 0.39) accepts reference `image`s for
  editing and exposes **no `mask_image` parameter**. diffusers ships a
  mask-conditioned FLUX.2 inpaint pipeline only for **FLUX.2 [klein]**
  (`Flux2KleinInpaintPipeline`, Qwen3 text encoder — not loadable with [dev]
  weights).
- The inpainting cases therefore ran through the official instruction-editing
  path: the frozen source as the single reference image plus a fixed wrapper
  prompt (`"Edit this photo: <frozen prompt>. Change nothing else in the image."`).
- Measured against the frozen masks, that path **re-renders the whole image**:
  outside-mask MAE 9.6–67.2 (FLUX.1-Fill-dev on the identical cases: 1.0–6.1),
  and on three cases the change *outside* the mask exceeds the change *inside*
  it. There is no mask confinement and no pixel-faithful background, so
  `bbox_from_mask` Mask_Region auto-annotation cannot be driven from this path.

## Rubric scores (protocol §5)

Scoring method matches the flux.1-dev run: anchored on the objective proxies
below (outside-mask MAE → background preservation; in-mask vs outside-mask
change → mask adherence) plus spot inspection. Per protocol §5 the rubric is
authoritative and a human reviewer should confirm before the Phase G decision;
**defect-realism and prompt-fidelity scores are provisional** (the editing path
does render a defect in the scene, but the whole-frame re-render makes
case-level realism/fidelity judgements unreliable without human review).

| case_id | mask adherence | background preservation | defect realism | prompt fidelity |
|---|---|---|---|---|
| inpaint-001 (scratch, ~3%) | 1 | 1 | 3 | 3 |
| inpaint-002 (corrosion, ~9%) | 2 | 2 | 3 | 3 |
| inpaint-003 (dent, ~4%) | 2 | 2 | 3 | 3 |
| inpaint-004 (weld porosity, ~6%) | 2 | 2 | 3 | 3 |
| inpaint-005 (paint chip, ~19%) | 2 | 2 | 3 | 3 |
| inpaint-006 (crack, ~2%) | 1 | 1 | 3 | 3 |
| inpaint-101 (cookie edge, ~11%) | 2 | 2 | 3 | 3 |
| inpaint-102 (cookie burn, ~8%) | 2 | 3 | 3 | 3 |
| inpaint-103 (cookie crack, ~2%) | 1 | 1 | 3 | 3 |

**Means: mask adherence 1.7 · background preservation 1.8 · defect realism 3.0 · prompt fidelity 3.0**

(FLUX.1-dev + Fill-dev on the same cases: 4.7 / 4.4 / 3.7 / 3.7.)

## Objective proxies (informational, do not override rubric)

MAE on the 0–255 scale vs the frozen source; ring = 24 px dilation band just
outside the mask. `flux1 out_MAE` repeats the FLUX.1-Fill-dev column from
`../../flux.1-dev/run-001/notes.md` for direct comparison.

| case | outside-mask MAE | ring MAE | in-mask MAE | mask % | flux1 out_MAE |
|---|---|---|---|---|---|
| inpaint-001 | 67.18 | 54.73 | 53.98 | 3.0 | 4.64 |
| inpaint-002 | 40.66 | 47.51 | 50.65 | 8.9 | 3.08 |
| inpaint-003 | 37.78 | 33.91 | 45.29 | 3.9 | 2.58 |
| inpaint-004 | 42.19 | 48.51 | 72.25 | 6.3 | 1.58 |
| inpaint-005 | 16.16 | 22.64 | 45.19 | 18.8 | 3.98 |
| inpaint-006 | 53.75 | 37.56 | 42.93 | 1.7 | 1.03 |
| inpaint-101 | 43.92 | 63.28 | 57.96 | 10.9 | 6.11 |
| inpaint-102 | 9.59 | 26.13 | 44.58 | 7.9 | 5.60 |
| inpaint-103 | 52.35 | 45.39 | 44.43 | 1.7 | 5.81 |

Reading: in-mask change is comparable to outside-mask change on every case
(ratio ≈ 0.8–2.8, vs 4–70 for Fill-dev) — the edit is global, not localized.
inpaint-102 is the best case (outside MAE 9.6) and inpaint-001/006/103 the
worst (52–67), i.e. small masks fare worst because the model has no signal
telling it to leave the rest alone.

## Failure notes

- **Attempt 1 — bf16 + `enable_model_cpu_offload`** (the plan's first attempt,
  matching the flux1-r1 host-RAM lesson): **CUDA OOM** on the first t2i case.
  `offload-probe-metrics.json` holds the record (`t2i-001` failed after 6.90 s,
  "Tried to allocate 1.25 GiB … 491 MiB free of 44.39 GiB"). Root cause: the
  32B DiT alone is ~64 GB in bf16, so no module-level offload split fits a
  48 GB L40S — host RAM was not the binding constraint here, VRAM was.
- **Attempt 2 — NF4 quantization** (officially documented diffusers
  quantization path, per the Req 2.9 escalation order: quantized path before
  any instance-size escalation) succeeded for all 13 cases. No approval for a
  larger instance was needed and none was requested.
- No per-case failures; the failure-isolation path was not exercised in the
  successful run (it was in attempt 1, where all 4 cases were recorded and the
  loop continued).

## Observations for the hosting comparison / cost model

- ~81 s per 768² edit and ~75 s per 1024² t2i at 28 steps under NF4+offload —
  roughly **2.2× slower than FLUX.1-Fill-dev** (37 s) on the same box, for a
  path that cannot do mask-constrained inpainting. On latency-per-dollar FLUX.2
  [dev] is the worst medium/large candidate benchmarked so far.
- Fitting FLUX.2 [dev] on one 48 GB GPU requires 4-bit quantization; a
  production-quality bf16 deployment needs ≥96 GB (multi-L40S or A100/H100
  80 GB class), which the cost model should price as the realistic shape.
- Licensing: FLUX [dev] Non-Commercial — Legal_Review_Flag already recorded in
  the Evaluation_Matrix; combined with the missing mask path this makes FLUX.2
  [dev] hard to recommend for the primary path.
- Worth a note for Phase G: **FLUX.2 [klein]** (Apache 2.0, 4B/9B) is the only
  FLUX.2 family member with a real mask inpaint pipeline in diffusers
  (`Flux2KleinInpaintPipeline`). It was not benchmarked under this task (out of
  scope for the large class) and is a candidate for a follow-up run.
