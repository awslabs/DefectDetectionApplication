# PixArt-Sigma — run-001 notes

Run: `pixart-sigma-r1` · g5.xlarge (A10G 24GB) · 2026-08-17 · protocol §3/§5

## Summary

- **model_load_seconds:** 554.0 (includes full HF download on a fresh instance)
- **Per-image latency (t2i, steady state, 1024×1024, 20 steps):** 6.84–7.09 s
  (t2i-001 22.03 s first case includes CUDA warm-up; t2i-002 6.84, t2i-003 7.09, t2i-004 7.08)
- **Cold_Start_Time proxy (model_load_seconds + first-case latency):** 554.0 + 22.0 ≈ 576.1 s
- **Inpainting:** all 9 cases `failed / unsupported_task` — PixArtSigmaPipeline
  has no inpainting path (matches Evaluation_Matrix: inpainting weak/unsupported).

## Rubric scores (protocol §5)

No inpainting outputs were produced (unsupported_task per the Evaluation_Matrix),
so there are no mask-adherence / background-preservation / defect-realism /
prompt-fidelity scores for this run. Rubric table intentionally empty.

| case_id | mask adherence | background preservation | defect realism | prompt fidelity |
|---|---|---|---|---|
| — | — | — | — | — |

## Text-to-image observations (non-rubric, informational)

_To be filled by human review of `outputs/` (t2i-001, t2i-003, t2i-004 committed;
full set in the benchmark bucket until teardown)._

## Failure notes

- 9 × inpainting: `RuntimeError: unsupported_task: no inpainting pipeline for this model` (expected).
- Operational (not case-level): the first Sigma attempt on the pixart-r1
  instance hit CUDA OOM from a stale GPU process, and a retry was cut short
  when that instance self-terminated (OS-initiated shutdown) during the heavy
  swap-backed T5 load. A fresh g5.xlarge with the 48 GiB swapfile enabled
  before the run completed Sigma on the first attempt (this run).
- Same host-RAM takeaway as PixArt-alpha: 16 GiB is insufficient for the
  T5-XXL encoder load without swap; recommend ≥32 GiB host RAM for hosting.
