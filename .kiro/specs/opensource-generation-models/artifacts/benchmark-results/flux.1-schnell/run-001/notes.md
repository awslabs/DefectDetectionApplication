# FLUX.1-schnell — run-001 notes

Run: `flux1schnell-r1` · g6e.8xlarge (L40S 48GB) · 2026-08-17 · protocol §3/§5
Inpainting via the **community** path (generic diffusers `FluxInpaintPipeline`,
strength 0.85, 4 steps) — no official Fill variant exists for schnell; the
Evaluation_Matrix records this path as community-grade, and this run measures
the gap vs FLUX.1-Fill-dev.

## Summary

- **13/13 cases ok** (9 inpainting + 4 t2i), zero failures — the community
  pipeline executed every case (quality gap captured in the rubric, not as
  case failures).
- **model_load_seconds:** 4.18 total (1.57 inpaint + 2.61 t2i; warm HF cache,
  bf16 + `enable_model_cpu_offload`).
- **Per-image latency, inpainting (4 steps, strength 0.85, 768×768):**
  20.1–26.4 s (first case 26.36 s, steady state ≈ 20.3 s).
- **Per-image latency, t2i (4 steps, 1024×1024):** 20.2–22.6 s.
- **Cold_Start_Time proxy (model_load_seconds + first-case latency):**
  4.18 + 26.36 ≈ **30.5 s** on a warm weights cache (add ~450 s weights
  download on a truly cold instance).
- Note: at 4 steps the latency is offload-bound, not compute-bound — schnell
  under cpu_offload is only ~1.8× faster than 50-step Fill-dev despite 12.5×
  fewer steps. Resident-weights hosting would widen the schnell speed advantage
  substantially.
- Full outputs in `s3://opensource-genmodels-benchmark-164152369890/runs/flux1schnell-r1/`
  (until Phase D teardown); representative copies in `outputs/`
  (inpaint-001, inpaint-005, inpaint-102, t2i-001, t2i-004).

## Rubric scores (protocol §5)

Scoring method: anchored on the objective proxies below plus spot inspection
of representative outputs; human confirmation recommended before Phase G
(same caveat as the flux.1-dev run notes).

| case_id | mask adherence | background preservation | defect realism | prompt fidelity |
|---|---|---|---|---|
| inpaint-001 (scratch, ~3%) | 4 | 5 | 3 | 3 |
| inpaint-002 (corrosion, ~9%) | 5 | 5 | 3 | 3 |
| inpaint-003 (dent, ~4%) | 5 | 5 | 3 | 3 |
| inpaint-004 (weld porosity, ~6%) | 4 | 5 | 3 | 3 |
| inpaint-005 (paint chip, ~19%) | 4 | 5 | 3 | 3 |
| inpaint-006 (crack, ~2%) | 4 | 5 | 3 | 3 |
| inpaint-101 (cookie edge, ~11%) | 3 | 4 | 3 | 3 |
| inpaint-102 (cookie burn, ~8%) | 3 | 4 | 3 | 3 |
| inpaint-103 (cookie crack, ~2%) | 3 | 4 | 3 | 3 |

**Means: mask adherence 3.9 · background preservation 4.7 · defect realism 3.0 · prompt fidelity 3.0**

## Objective proxies (informational, do not override rubric)

| case | outside-mask MAE | ring MAE | in-mask MAE |
|---|---|---|---|
| inpaint-001 | 2.11 | 4.45 | 60.63 |
| inpaint-002 | 0.83 | 2.33 | 37.33 |
| inpaint-003 | 1.39 | 2.13 | 29.47 |
| inpaint-004 | 1.22 | 4.66 | 59.44 |
| inpaint-005 | 1.83 | 5.73 | 59.94 |
| inpaint-006 | 1.34 | 2.09 | 15.64 |
| inpaint-101 | 4.49 | 11.79 | 88.15 |
| inpaint-102 | 3.88 | 9.43 | 77.10 |
| inpaint-103 | 4.29 | 10.38 | 49.72 |

Reading vs Fill-dev: schnell's latent-noising inpaint preserves far background
slightly better (lower far MAE) but shows consistently elevated ring values —
visible seams / halo at the mask boundary, worst on the real cookie photos
(ring ≈ 2–3× far). Edits are aggressive in-mask (strength 0.85), but 4-step
timestep-distilled sampling with guidance ignored limits defect specificity —
the model fills plausibly textured content that tracks the prompt loosely.
This quantifies the expected community-vs-official gap: schnell trails Fill-dev
mainly on mask-boundary adherence and prompt-controlled defect realism.

## Failure notes

None — all 13 cases completed. Same memory posture as the dev run
(sequential task-group loads, cpu_offload; full `.to("cuda")` OOMs at load).

## Observations for the hosting comparison

- Apache 2.0 license — the only license-clean FLUX.1 candidate; the measured
  quality gap vs Fill-dev is the price of avoiding the non-commercial license.
- Same host-RAM sizing lesson as flux.1-dev: ≥64 GiB host RAM per resident
  bf16 FLUX pipeline under cpu_offload.
