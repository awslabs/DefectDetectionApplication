# PixArt-alpha — run-001 notes

Run: `pixart-alpha-r1` · g5.xlarge (A10G 24GB) · 2026-08-17 · protocol §3/§5

## Summary

- **model_load_seconds:** 375.8 (warm HF cache; first load with download was 817.4)
- **Per-image latency (t2i, steady state, 1024×1024, 20 steps):** 6.39–8.65 s
  (t2i-001 8.65 s first case after load, t2i-002 6.39, t2i-003 6.71, t2i-004 6.64)
- **Cold_Start_Time proxy (model_load_seconds + first-case latency):** 375.8 + 8.65 ≈ 384.5 s
- **Inpainting:** all 9 cases `failed / unsupported_task` — PixArtAlphaPipeline
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
- Operational (not case-level): first driver attempt host-OOM-killed loading the
  fp32 T5-XXL text encoder on 16 GiB RAM — fixed with a 48 GiB swapfile. A
  second attempt (with PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True) was
  killed during load; a plain rerun on an idle GPU completed cleanly. A stale
  GPU process from the killed attempt caused transient CUDA OOM on two t2i
  cases in an intermediate run; the final clean run recorded here has 4/4 t2i ok.
- Takeaway for the hosting comparison: g5.xlarge (16 GiB host RAM) is
  insufficient to load the PixArt T5-XXL encoder without swap; recommend
  ≥32 GiB host RAM (e.g., g5.2xlarge / g6.2xlarge) or fp16 text-encoder loading.
