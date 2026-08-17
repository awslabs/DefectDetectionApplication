# Notes — pixart-sigma-r1 (g5.xlarge, 2026-08-17)

## Rubric applicability

PixArt-Sigma is **text_to_image only** (Evaluation_Matrix: inpainting path
`community`, `PixArtSigmaPipeline` is T2I-only). All 9 inpainting cases are
recorded `failed / unsupported_task` per protocol §3 step 5. The four-axis
inpainting rubric does not apply — no mask axes scored; quality is recorded
as a realism / prompt-fidelity observation on the t2i outputs.

## t2i output observations (4/4 ok, seeds 201–204)

- **t2i-001** (scratched metal plate, top-down): crisper surface detail than
  the alpha output on the same seed/prompt; scratch geometry plausible;
  lighting more controlled.
- **t2i-002** (corroded steel, rust patches): strong — rust texture and
  color variation read realistically; on par with or slightly above alpha.
- **t2i-003** (cracked plastic housing, macro): crack structure better
  defined than alpha; macro cue partially honored.
- **t2i-004** (broken cookie on conveyor): pieces and crumb texture
  recognizable; scene composition still drifts from a strict top-down
  factory shot — same fidelity weakness as alpha.

Overall: modest quality edge over PixArt-alpha at essentially identical cost
and latency. Still cannot serve the pipeline's primary source+mask+prompt
inpainting path — confirms the matrix's "cheap T2I-only data point" role.

## Latency / cold start

- `model_load_seconds`: **567.4** (sigma DiT + its own T5-XXL copy pulled
  from HF on this instance's cache; dominated by download/disk, not GPU).
- First-case t2i latency: **9.8 s** (t2i-001, includes warmup);
  steady-state: **6.8–7.1 s** per 1024×1024 image (20 steps, bf16, A10G).
- Cold_Start_Time proxy (protocol §3): 567.4 + 9.8 ≈ **577 s**.

## Failure notes

- 9/9 inpainting cases: `RuntimeError: unsupported_task` — expected; case
  loop continued through all remaining cases (Property 5 failure isolation
  held in production).
- **Run-attempt history:** attempts 1–2 failed all four t2i cases with CUDA
  OOM. Root cause: a stray duplicate pixart-alpha `run_driver.py` process
  (launched outside this session) was holding 11–15 GB of the A10G's 22 GB.
  The OOM was environmental, not a model-capacity issue — the sigma pipeline
  fits comfortably in ~11 GB. Stray process killed; attempt 3 (final
  metrics.json) ran clean with kill + run in a single atomic SSM command.
