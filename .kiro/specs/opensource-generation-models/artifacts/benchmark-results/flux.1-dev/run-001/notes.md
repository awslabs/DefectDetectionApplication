# FLUX.1-dev (+ FLUX.1-Fill-dev) — run-001 notes

Run: `flux1dev-r1` · g6e.8xlarge (L40S 48GB) · 2026-08-17 · protocol §3/§5
Inpainting via **FLUX.1-Fill-dev / FluxFillPipeline** (official variant per the
Evaluation_Matrix); text-to-image via base FLUX.1-dev / FluxPipeline.

## Summary

- **13/13 cases ok** (9 inpainting + 4 t2i), zero failures.
- **model_load_seconds:** 4.44 total (1.58 Fill-dev + 2.86 dev; warm HF cache,
  bf16 + `enable_model_cpu_offload`). First-download times measured separately:
  dev 450.2 s (~58 GB), Fill-dev 445.6 s (~55 GB) on g6e.8xlarge networking.
- **Per-image latency, inpainting (Fill-dev, 50 steps, 768×768):** 36.4–40.4 s
  (first case 40.41 s, steady state ≈ 36.7 s).
- **Per-image latency, t2i (dev, 28 steps, 1024×1024):** 34.5–37.0 s.
- **Cold_Start_Time proxy (model_load_seconds + first-case latency):**
  4.44 + 40.41 ≈ **44.9 s** on a warm weights cache. On a truly cold instance
  add the weights download (≈450 s/model) — dominated by the ~55–58 GB repos.
- Full outputs in `s3://opensource-genmodels-benchmark-164152369890/runs/flux1dev-r1/`
  (until Phase D teardown); representative copies in `outputs/`
  (inpaint-001, inpaint-005, inpaint-102, t2i-001, t2i-004).

## Rubric scores (protocol §5)

Scoring method: scores are anchored on the objective proxies below
(outside-mask MAE = background preservation; near-mask ring MAE vs far MAE =
mask adherence / spill; in-mask MAE = edit magnitude) plus spot inspection of
the representative outputs. Per protocol §5 the rubric is authoritative and a
human reviewer should confirm before the Phase G decision; treat defect-realism
and prompt-fidelity scores as provisional.

| case_id | mask adherence | background preservation | defect realism | prompt fidelity |
|---|---|---|---|---|
| inpaint-001 (scratch, ~3%) | 5 | 4 | 4 | 4 |
| inpaint-002 (corrosion, ~9%) | 5 | 5 | 4 | 4 |
| inpaint-003 (dent, ~4%) | 5 | 5 | 4 | 4 |
| inpaint-004 (weld porosity, ~6%) | 5 | 5 | 3 | 3 |
| inpaint-005 (paint chip, ~19%) | 5 | 4 | 4 | 4 |
| inpaint-006 (crack, ~2%) | 5 | 5 | 3 | 3 |
| inpaint-101 (cookie edge, ~11%) | 4 | 4 | 4 | 4 |
| inpaint-102 (cookie burn, ~8%) | 4 | 4 | 4 | 4 |
| inpaint-103 (cookie crack, ~2%) | 4 | 4 | 3 | 3 |

**Means: mask adherence 4.7 · background preservation 4.4 · defect realism 3.7 · prompt fidelity 3.7**

## Objective proxies (informational, do not override rubric)

MAE on 0–255 scale vs the frozen source; ring = 24 px dilation band just
outside the mask.

| case | outside-mask MAE | ring MAE | in-mask MAE |
|---|---|---|---|
| inpaint-001 | 4.64 | 5.88 | 40.82 |
| inpaint-002 | 3.08 | 2.86 | 26.50 |
| inpaint-003 | 2.58 | 3.04 | 19.21 |
| inpaint-004 | 1.58 | 1.77 | 6.33 |
| inpaint-005 | 3.98 | 4.15 | 41.52 |
| inpaint-006 | 1.03 | 1.21 | 6.42 |
| inpaint-101 | 6.11 | 8.57 | 76.24 |
| inpaint-102 | 5.60 | 8.10 | 106.75 |
| inpaint-103 | 5.81 | 8.67 | 33.50 |

Reading: strong in-mask edits everywhere (in-mask MAE 6–107); ring ≈ far on
every synthetic case (no spill outside the mask); cookie photos show a mild
global VAE-roundtrip shift (far MAE ≈ 6) and slightly elevated ring values —
still comfortably background-faithful. Low in-mask deltas on 004/006 mean the
inserted weld-porosity/crack is subtle (dark thin defects on dark seams),
consistent with the lower realism/fidelity scores.

## Failure notes

None — all 13 cases completed. No CUDA OOM with sequential task-group loading
+ `enable_model_cpu_offload` (a full `.to("cuda")` of either FLUX pipeline
OOMs the 48 GB L40S at load; recorded in the driver spec).

## Observations for the hosting comparison

- Fill-dev at 50 steps ≈ 37 s/image under cpu_offload — a resident-weights
  hosting shape (no offload, e.g. 2×GPU or quantized) should be materially
  faster; treat 37 s as the offload-mode upper bound for the medium class.
- Host RAM, not VRAM, was the sizing constraint: 32 GiB (g6e.xlarge) cannot
  hold a ~34 GB bf16 FLUX pipeline under cpu_offload. Recommend ≥64 GiB host
  RAM per resident FLUX pipeline or quantized weights.
- Licensing: both dev and Fill-dev are non-commercial (Legal_Review_Flag
  already recorded in the Evaluation_Matrix / Phase G input).
