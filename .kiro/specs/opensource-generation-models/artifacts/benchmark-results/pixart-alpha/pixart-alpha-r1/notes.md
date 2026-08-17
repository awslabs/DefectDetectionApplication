# Notes — pixart-alpha-r1 (g5.xlarge, 2026-08-17)

## Rubric applicability

PixArt-alpha is **text_to_image only** (Evaluation_Matrix: inpainting path
`community` / no official pipeline; harness records all 9 inpainting cases as
`failed / unsupported_task` per protocol §3 step 5). The four-axis inpainting
rubric (mask adherence / background preservation / defect realism / prompt
fidelity) therefore does not apply — no mask axes are scored. Per the task
note, quality is recorded as a realism / prompt-fidelity observation on the
t2i outputs only.

## t2i output observations (4/4 ok, seeds 201–204)

- **t2i-001** (scratched metal plate, top-down): plausible brushed-metal
  surface with visible linear scratches; harsh-lighting cue partially
  honored. Usable as a generic defect texture; not photorealistic at
  inspection-camera fidelity.
- **t2i-002** (corroded steel, rust patches): strong texture realism — rust
  patch distribution reads naturally; best of the four for realism.
- **t2i-003** (cracked plastic housing, macro): crack rendered but "macro /
  shallow depth of field" only loosely followed; plastic material reads
  slightly synthetic.
- **t2i-004** (broken cookie on conveyor): cookie pieces recognizable,
  conveyor context approximate; weakest prompt fidelity of the set (scene
  composition drifts from "top-down factory inspection").

Overall: acceptable generic-texture T2I at very low cost, but the model
cannot serve the pipeline's primary source+mask+prompt path — consistent with
the matrix's "cheap T2I-only data point" finding.

## Latency / cold start

- `model_load_seconds`: **853.0** (includes first-boot HF download of the
  T5-XXL encoder + DiT weights on a cold cache — dominated by network, not
  GPU).
- First-case t2i latency: **20.1 s** (t2i-001, includes CUDA graph/kernel
  warmup); steady-state t2i latency: **6.4–6.7 s** per 1024×1024 image
  (20 denoising steps, bf16, A10G).
- Cold_Start_Time proxy (protocol §3): 853.0 + 20.1 ≈ **873 s** on a fresh
  instance with cold HF cache.

## Failure notes

- 9/9 inpainting cases: `RuntimeError: unsupported_task` — expected,
  recorded per Req 2.10; the case loop continued through all remaining cases
  (failure isolation held in production, matching Property 5).
- `clean_caption` warning (beautifulsoup4 absent) — cosmetic; diffusers falls
  back to raw prompts; prompts in cases.json are already clean strings.
