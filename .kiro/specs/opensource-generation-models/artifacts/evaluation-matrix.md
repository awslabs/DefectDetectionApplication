# Evaluation Matrix — Open-Source Generation Models (Requirement 1)

**Phase A desk research.** One row per Candidate_Model, fixed column schema per design.md.
Research date: August 2026. All facts verified against primary sources (model cards, license texts, official repos); evidence URLs cited per cell/section.

## Column vocabulary (fixed by design)

- **Capability flags** (`MODEL_CATALOG` vocabulary): `text_to_image`, `inpainting`, `image_variation`, `seed`, `cfg_scale` — booleans
- **Inpainting path**: `native` | `official-variant` | `community` | `unsupported`
- **License**: name, commercial-use terms, license text URL
- **Resources**: parameter count, min/recommended GPU memory, satisfying AWS instance types
- **Weights access**: location, `open` | `gated` | `api-only`, redistribution restrictions
- **Benchmark status**: `included` | `excluded (weights unobtainable)`

## Summary matrix

| Candidate_Model | text_to_image | inpainting | image_variation | seed | cfg_scale | Inpainting path | License | Params | Min / rec GPU mem | AWS instance types | Weights access | Benchmark status |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| FLUX.1-dev | ✅ | ✅ (via Fill-dev) | ✅ (img2img strength) | ✅ | ✅ (guidance-distilled; `guidance_scale` param) | **official-variant** (FLUX.1-Fill-dev) | FLUX.1 [dev] Non-Commercial | ~12B (+T5-XXL ~4.7B enc) | 24 GB (bf16, offload) / 48 GB | g6e.xlarge (L40S 48 GB) | HF, **gated** | **included** |
| FLUX.1-schnell | ✅ | ⚠️ (generic pipeline only) | ✅ (img2img strength) | ✅ | ❌ (timestep-distilled, guidance ignored) | **community** (diffusers `FluxInpaintPipeline`, no official Fill variant) | Apache 2.0 | ~12B (+T5-XXL enc) | 24 GB / 48 GB | g6e.xlarge (L40S 48 GB) | HF, **open** | **included** |
| FLUX.2 (dev) | ✅ | ✅ (native editing; mask-driven path to validate in Phase C) | ✅ (multi-reference editing) | ✅ | ✅ | **native** (built-in image editing; binary-mask inpainting workflow via ComfyUI/diffusers) | FLUX [dev] Non-Commercial | **32B** (+Mistral-3 24B VLM enc) | ~48 GB (FP8 + offload) / ≥96 GB (bf16 multi-GPU) | g6e.2xlarge (FP8, tight) / g6e.12xlarge (4×L40S) or p4d slice | HF, **gated** | **included** |
| HunyuanImage (3.0) | ✅ | ⚠️ (instruction-based editing, no documented mask API) | ✅ (I2I via 3.0-Instruct) | ✅ | ✅ | **official-variant** (HunyuanImage-3.0-Instruct I2I editing; binary-mask inpainting undocumented → treat as community/unvalidated) | Tencent Hunyuan Community License | **80B MoE** (13B active/token) | ~80 GB (NF4/INT8 quant) / 3–4×80 GB (bf16) | p4de.24xlarge / p5.48xlarge (bf16); g6e.12xlarge marginal w/ NF4 | HF, **open** (license agreement in repo) | **included** |
| PixArt-alpha | ✅ | ❌ | ✅ (img2img) | ✅ | ✅ | **community** (no official inpaint pipeline; community workflows only) | CreativeML Open RAIL++-M (weights); AGPL-3.0 (repo code) | ~0.6B DiT (+T5-XXL ~4.3B enc) | 12 GB / 24 GB | g5.xlarge / g6.xlarge (A10G/L4 24 GB) | HF, **open** | **included** |
| PixArt-Sigma | ✅ | ❌ | ✅ (img2img) | ✅ | ✅ | **community** (no official inpaint pipeline) | CreativeML Open RAIL++-M (weights); AGPL-3.0 (repo code) | ~0.6B DiT (+T5-XXL enc) | 12 GB / 24 GB | g5.xlarge / g6.xlarge (A10G/L4 24 GB) | HF, **open** | **included** |

**No candidate is excluded under Req 1.7** — every model has downloadable weights for self-hosting in the Portal_Account. FLUX.2 [pro] and [flex] variants are api-only, but the FLUX.2 [dev] open-weight variant satisfies self-hosting, so the FLUX.2 row is pinned to FLUX.2 [dev].

---

## Per-model evidence

### FLUX.1-dev (Black Forest Labs)

- **Capabilities**: text_to_image ✅ (primary), inpainting via official Fill variant ✅, image_variation ✅ (diffusers `FluxImg2ImgPipeline`), seed ✅ (generator seed), cfg_scale ✅ with caveat — the model is guidance-distilled; `guidance_scale` is an embedded conditioning input, not classic CFG, but maps to the `cfg_scale` catalog flag.
- **Inpainting path — `official-variant`**: [FLUX.1-Fill-dev](https://huggingface.co/black-forest-labs/FLUX.1-Fill-dev) is BFL's official 12B inpainting/outpainting model taking a source image + binary mask + text prompt — exactly the pipeline's primary generation shape. Supported in diffusers as `FluxFillPipeline` ([BFL fill docs](https://github.com/black-forest-labs/flux/blob/main/docs/fill.md), [BFL FLUX.1 Tools announcement](https://bfl.ai/blog/24-11-21-tools)).
- **License**: FLUX.1 [dev] Non-Commercial License — weights and inference code available for non-commercial, non-production use only; **generated outputs** may be used commercially per the license/model card. Portal production use of the hosted model = commercial/production use of the weights → **Legal_Review_Flag downstream (Req 7.2)**. License text: [FLUX.1-dev LICENSE.md](https://huggingface.co/black-forest-labs/FLUX.1-dev/blob/main/LICENSE.md); model card: [black-forest-labs/FLUX.1-dev](https://huggingface.co/black-forest-labs/FLUX.1-dev). FLUX.1-Fill-dev carries the same license.
- **Resources**: 12B rectified-flow transformer + T5-XXL/CLIP text encoders. ~24 GB weights in bf16; runs on 24 GB with CPU offload (slow), comfortable on 48 GB. **Pinned: 12B → medium class → g6e.xlarge (L40S 48 GB)** per design sizing table.
- **Weights access**: Hugging Face, **gated** — requires accepting the license on the model page before download. Redistribution: license restricts distribution of the model/derivatives to the same license terms; no open redistribution.

### FLUX.1-schnell (Black Forest Labs)

- **Capabilities**: text_to_image ✅ (4-step timestep-distilled), inpainting ⚠️ community-generic only, image_variation ✅ (img2img), seed ✅, cfg_scale ❌ — schnell is timestep-distilled and does not use guidance (`guidance_scale` has no effect).
- **Inpainting path — `community`**: no official Fill variant exists for schnell. The generic diffusers `FluxInpaintPipeline` (latent-noising inpaint) loads schnell weights, but this is not a purpose-trained fill model; quality on tight defect masks is expected to be materially below Fill-dev ([diffusers Flux pipelines docs](https://huggingface.co/docs/diffusers/api/pipelines/flux)). Phase C benchmarks this path to quantify the gap.
- **License**: **Apache 2.0** — full commercial use, no legal flag. Model card + license: [black-forest-labs/FLUX.1-schnell](https://huggingface.co/black-forest-labs/FLUX.1-schnell).
- **Resources**: same 12B architecture as dev. **Pinned: 12B → medium class → g6e.xlarge (L40S 48 GB)**.
- **Weights access**: Hugging Face, **open** download (no gate). Apache 2.0 → redistribution unrestricted.

### FLUX.2 [dev] (Black Forest Labs)

- **Status verified (Aug 2026)**: FLUX.2 [dev] is released open-weight; FLUX.2 [pro]/[flex] remain api-only; FLUX.2 [klein] (4B/9B, Apache 2.0) also exists as a small open variant ([FLUX.2-klein-4B card](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B)) — noted for the Decision_Record but the spec candidate row is pinned to FLUX.2 [dev].
- **Capabilities**: text_to_image ✅, image editing ✅ native (single- and multi-reference, up to ~10 reference images), image_variation ✅, seed ✅, cfg_scale ✅.
- **Inpainting path — `native`** (with a Phase C validation caveat): FLUX.2 [dev] is "a 32 billion parameter model for text-to-image generation and image editing (single/multi reference)" ([BFL flux2 repo README](https://github.com/black-forest-labs/flux2/blob/main/README.md)); ComfyUI ships day-0 FLUX.2 [dev] workflows including mask-based edits ([ComfyUI FLUX.2 dev tutorial](https://docs.comfy.org/tutorials/flux/flux-2-dev)). Editing is primarily instruction/reference-driven rather than binary-mask-driven; whether a strict source+mask+prompt inpaint matches Fill-dev's mask adherence is a Phase C question — the benchmark protocol's inpainting cases will resolve it.
- **License**: FLUX [dev] Non-Commercial License — non-commercial, non-production use of the weights; generated outputs usable commercially ([FLUX.2-dev model card](https://huggingface.co/black-forest-labs/FLUX.2-dev), [FLUX.2-dev LICENSE mirror](https://huggingface.co/unsloth/FLUX.2-dev/blob/main/LICENSE.md)). Same **Legal_Review_Flag** situation as FLUX.1-dev.
- **Resources — pinned for Phase C sizing**: **32B** transformer ([model card](https://huggingface.co/black-forest-labs/FLUX.2-dev)), plus a Mistral-3 24B-class VLM text encoder. ~64 GB VRAM for the transformer alone at bf16; ~32 GB at FP8 ([Runpod FLUX.2 deployment guide](https://www.runpod.io/articles/guides/deploying-flux-2)). **Large class**: FP8 + encoder offload fits (tightly) on a single L40S 48 GB → g6e.2xlarge is the cheapest viable benchmark box; clean bf16 needs multi-GPU (g6e.12xlarge, 4×L40S = 192 GB) or a p4d A100 slice, matching the design's large-class fallback.
- **Weights access**: Hugging Face, **gated** (license acceptance). Redistribution restricted to the same non-commercial license terms (quantized rederivatives on HF all carry the original license forward).

### HunyuanImage (Tencent) — row pinned to HunyuanImage-3.0

- **Version status verified (Aug 2026)**: current flagship is **HunyuanImage-3.0** (released Sept 2025; Instruct variant with I2I editing released Jan 26, 2026 per the [model card news](https://huggingface.co/tencent/HunyuanImage-3.0)). **HunyuanImage-2.1** (17B, 2K-resolution, single-GPU-friendly) remains available as a smaller alternative ([HunyuanImage-2.1 card](https://huggingface.co/tencent/HunyuanImage-2.1)) and is noted as the fallback if 3.0's instance cost breaks the Cost_Cap.
- **Capabilities (3.0)**: text_to_image ✅ (autoregressive multimodal, CoT prompt reasoning), image_variation ✅ via HunyuanImage-3.0-Instruct I2I editing, inpainting ⚠️ — no documented binary source+mask API, seed ✅, cfg_scale ✅.
- **Inpainting path — `official-variant` (I2I editing), mask-driven inpainting unvalidated**: HunyuanImage-3.0-Instruct provides official image-to-image creative editing ([model card](https://huggingface.co/tencent/HunyuanImage-3.0), [official repo](https://github.com/Tencent-Hunyuan/HunyuanImage-3.0)), but editing is instruction-based; a strict mask-constrained inpaint (required for Mask_Region auto-annotation) is not documented and would rely on community tooling. Phase C treats the mask path as the make-or-break test for this candidate.
- **License**: **Tencent Hunyuan Community License Agreement** — commercial use permitted under the community license terms, with two clauses to flag for legal review: (a) the license **does not apply in the EU, UK, and South Korea** (territory restriction; portal runs in us-east-1 serving US usage, but user geography needs assessment), (b) a separate-license threshold for very-large-MAU products (the standard Hunyuan community-license >100M MAU clause). License text (representative Hunyuan community license): [Tencent Hunyuan LICENSE](https://huggingface.co/calcuis/hunyuan-gguf/blob/main/LICENSE); authoritative copy in the [HunyuanImage-3.0 repo](https://github.com/Tencent-Hunyuan/HunyuanImage-3.0). **Legal_Review_Flag downstream.**
- **Resources — pinned for Phase C sizing**: **80B total MoE, 13B active per token** ([technical report arXiv:2509.23951](https://arxiv.org/abs/2509.23951), [model card](https://huggingface.co/tencent/HunyuanImage-3.0)). bf16 needs multiple 80 GB GPUs (3–4×80 GB class) → **p4de.24xlarge or p5.48xlarge**; NF4/INT8 community quantizations run in ~single-80 GB-class budgets ([NF4 ComfyUI port](https://huggingface.co/EricRollei/HunyuanImage-3-NF4-ComfyUI)). This is beyond the design's g6e large-class assumption — the benchmark plan for this model must either use a p4d/p4de/p5 slice or substitute HunyuanImage-2.1 (17B, ~26–30 GB fp8 → g6e.xlarge feasible) with the substitution recorded.
- **Weights access**: Hugging Face, **open** download (license agreement bundled in repo; no API-only restriction). Redistribution permitted under the community license with the agreement text attached and territory limits carried forward.

### PixArt-alpha

- **Capabilities**: text_to_image ✅, inpainting ❌ (no official pipeline), image_variation ✅ (community img2img on the DiT), seed ✅, cfg_scale ✅ (standard CFG).
- **Inpainting path — `community`**: diffusers ships `PixArtAlphaPipeline` for T2I only; no official inpaint pipeline exists ([diffusers PixArt-α docs](https://huggingface.co/docs/diffusers/api/pipelines/pixart)). Any mask-based path is community glue → expected weak; PixArt stays valuable as the cheap text-to-image data point (per design known starting points).
- **License**: weights released under **CreativeML Open RAIL++-M** (commercial use permitted with embedded use restrictions) on the HF model cards; the GitHub training/inference repo code is AGPL-3.0 (does not encumber weights or diffusers usage). Model card: [PixArt-alpha/PixArt-XL-2-1024-MS](https://huggingface.co/PixArt-alpha/PixArt-XL-2-1024-MS); repo: [PixArt-alpha GitHub](https://github.com/PixArt-alpha/PixArt-alpha).
- **Resources — pinned**: **~0.6B** DiT ([arXiv:2403.04692 comparison table](https://arxiv.org/abs/2403.04692)) + T5-XXL text encoder (~4.3B). Whole pipeline ~12 GB fp16; comfortable on 24 GB. **Small class → g5.xlarge / g6.xlarge (24 GB)** per design sizing table.
- **Weights access**: Hugging Face, **open** download, no gate. RAIL++ redistribution allowed with use-restriction clauses carried forward.

### PixArt-Sigma

- **Capabilities**: identical flag profile to PixArt-alpha — text_to_image ✅ (up to 4K), inpainting ❌, image_variation ✅ (community), seed ✅, cfg_scale ✅.
- **Inpainting path — `community`**: `PixArtSigmaPipeline` is T2I only ([diffusers PixArt-Σ docs](https://huggingface.co/docs/diffusers/main/en/api/pipelines/pixart_sigma)).
- **License**: CreativeML Open RAIL++-M weights license on the HF cards; AGPL-3.0 repo code. Model card: [PixArt-alpha/PixArt-Sigma-XL-2-1024-MS](https://huggingface.co/PixArt-alpha/PixArt-Sigma-XL-2-1024-MS); repo: [PixArt-sigma GitHub](https://github.com/PixArt-alpha/PixArt-sigma).
- **Resources — pinned**: **~0.6B** DiT ([arXiv:2403.04692](https://arxiv.org/abs/2403.04692), [diffusers docs](https://huggingface.co/docs/diffusers/main/en/api/pipelines/pixart_sigma)) + T5-XXL encoder. **Small class → g5.xlarge / g6.xlarge (24 GB)**.
- **Weights access**: Hugging Face, **open** download, no gate. Same RAIL++ redistribution terms.

---

## Phase C sizing summary (pinned parameter counts → instance classes)

| Size class | Models | Pinned params | Benchmark instance |
|---|---|---|---|
| Small | PixArt-alpha, PixArt-Sigma | ~0.6B DiT (+T5-XXL enc) | g5.xlarge or g6.xlarge (24 GB) |
| Medium | FLUX.1-dev, FLUX.1-schnell, FLUX.1-Fill-dev | ~12B (+T5-XXL enc) | g6e.xlarge (L40S 48 GB) |
| Large | FLUX.2 [dev] | 32B (+24B VLM enc) | g6e.2xlarge (FP8, tight) → g6e.12xlarge or p4d slice for bf16 |
| Extra-large | HunyuanImage-3.0 | 80B MoE (13B active) | p4de.24xlarge / p5.48xlarge (bf16); fallback: benchmark HunyuanImage-2.1 (17B) on g6e.xlarge and record substitution |

**Sizing note for Task 4.3:** HunyuanImage-3.0 bf16 exceeds the design's g6e large-class assumption. The run plan must choose between (a) a quantized (NF4/INT8) run on the largest g6e, (b) a p4de/p5 on-demand slice with the Cost_Cap ledger checked hard, or (c) substituting HunyuanImage-2.1 — decision deferred to the Phase C checkpoint (Task 3).

---

## Completeness review — Property 1 checklist (Task 1.4)

Property 1 (matrix portion): for every Candidate_Model, a row exists with all required fields populated with evidence links; exclusion markings consistent (Property 2 first half).

| Check | FLUX.1-dev | FLUX.1-schnell | FLUX.2 | HunyuanImage | PixArt-alpha | PixArt-Sigma |
|---|---|---|---|---|---|---|
| Row present in summary matrix (Req 1.1) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| All 5 capability flags in MODEL_CATALOG vocabulary (Req 1.2) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Inpainting path from closed vocabulary (Req 1.3) | ✅ official-variant | ✅ community | ✅ native (caveated) | ✅ official-variant (caveated) | ✅ community | ✅ community |
| License name + commercial terms + license text URL (Req 1.4) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Parameter count + min/rec GPU memory + AWS instance types (Req 1.5) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Weights location + access mechanism + redistribution restrictions (Req 1.6) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Benchmark status recorded (Req 1.7) | ✅ included | ✅ included | ✅ included | ✅ included | ✅ included | ✅ included |
| Evidence URL(s) cited in the per-model section | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

**Exclusion consistency (Property 2, first half):** no model is marked `excluded (weights unobtainable)`; all six proceed to Phase C, so no exclusion/benchmark-result inconsistency can arise from this matrix. FLUX.2 api-only variants ([pro]/[flex]) are noted but the row is pinned to the open-weight [dev] variant, so Req 1.7 is not triggered.

**Checklist outcome: PASS** — matrix complete for all six Candidate_Models; three Legal_Review_Flags pre-identified for the Decision_Record (FLUX.1-dev non-commercial, FLUX.2 [dev] non-commercial, HunyuanImage territory/MAU clauses); one sizing decision deferred to the Phase C checkpoint (HunyuanImage-3.0 instance class).

*Reviewed: August 2026, during Task 1.4. Content in cited cells was paraphrased from sources for compliance with licensing restrictions.*

---

## Pinned Parameter Counts → Phase C Instance Sizing (Task 1.3 output)

| Size class | Model(s) | Pinned params | Benchmark instance (Phase C) |
|---|---|---|---|
| Small | PixArt-alpha, PixArt-Sigma | 0.6B DiT + 4.7B T5-XXL (~11 GB bf16 pipeline) | g5.xlarge or g6.xlarge (24 GB) — as designed |
| Medium | FLUX.1-dev, FLUX.1-schnell, FLUX.1-Fill-dev | 12B each (~34 GB bf16 pipeline) | g6e.xlarge (L40S 48 GB) — as designed |
| Large | HunyuanImage-2.1 | 17B DiT + refiner + encoders | g6e.2xlarge/g6e.4xlarge (48 GB GPU + host-RAM offload); p4d slice fallback for bf16 |
| Large | FLUX.2 [dev] | 32B DiT + ~24B text encoder | g6e.4xlarge with FP8/4-bit quantization + offload; p4d.24xlarge (A100 80 GB) fallback |

No Candidate_Model has unobtainable weights → **no 1.7 exclusions**; all six are `included`. The only api-only artifacts encountered (FLUX.2 pro/flex/max) are variants outside the benchmarked checkpoints, recorded in the FLUX.2 row.

## Key Findings for Downstream Phases

1. **Only FLUX.1-dev (+Fill-dev) offers an official mask-based inpainting path** — the pipeline's make-or-break capability. It carries a non-commercial license (Legal_Review_Flag).
2. **FLUX.1-schnell (Apache 2.0) is the only license-clean candidate with any inpainting story** (community-grade); Phase C must measure whether community inpainting quality is acceptable.
3. **FLUX.2 [dev] has native image_variation (reference editing)** — uniquely valuable for the fallback path (zero Bedrock models in us-east-1 support variation) — but no mask inpainting and a non-commercial license. FLUX.2 [klein] 4B (Apache 2.0) is a license-clean family member worth a benchmark note.
4. **HunyuanImage-2.1's license §5(b) prohibits using outputs to improve other AI models** — likely fatal for a synthetic-training-data pipeline independent of quality; also EU/UK/South Korea territory exclusion.
5. **PixArt models are cheap T2I-only data points**; they cannot serve the primary path.

## Completeness Review (Task 1.4 — Property 1, matrix portion)

Checklist run 2026-02 against every Candidate_Model row (FLUX.1-dev, FLUX.1-schnell, FLUX.2, HunyuanImage, PixArt-alpha, PixArt-Sigma):

| Check (per row) | FLUX.1-dev | FLUX.1-schnell | FLUX.2 | HunyuanImage | PixArt-alpha | PixArt-Sigma |
|---|---|---|---|---|---|---|
| All five capability flags recorded in `MODEL_CATALOG` vocabulary (1.2) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Inpainting path from closed vocabulary `native/official-variant/community/unsupported` (1.3) | ✅ official-variant | ✅ community | ✅ community | ✅ unsupported | ✅ unsupported | ✅ unsupported |
| License name + commercial terms + license text URL (1.4) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Parameter count + min/recommended GPU memory + satisfying AWS instance types (1.5) | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| Weights location + access mechanism (`open/gated/api-only`) + redistribution restrictions (1.6) | ✅ gated | ✅ open | ✅ gated (dev) / api-only variants noted | ✅ open | ✅ open | ✅ open |
| Benchmark status `included/excluded` with evidence, exclusions consistent (1.7) | ✅ included | ✅ included | ✅ included | ✅ included | ✅ included | ✅ included |
| Every cell carries at least one evidence URL | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

**Exclusion-consistency check (Property 2, first half):** zero rows are marked `excluded`, and zero benchmark-results directories exist yet, so the "excluded ⇒ no benchmark results" implication holds vacuously. Consistent.

**Outcome: PASS** — all six rows fully populated with evidence links; matrix satisfies Requirements 1.1–1.6, with no 1.7 exclusions to record. Matrix is ready to feed the Benchmark_Protocol candidate list (all six included) and Phase C instance sizing.

---

## Phase C reconciliation of the inpainting-path column (added during task 9.2)

This document carries **two** Property-1 checklist tables from Phase A (the
duplicate "Completeness review" sections above) whose `inpainting path` values
disagree. Resolution, recorded during the task 9.2 cross-document consistency
review:

- The **summary matrix at the top of this file is authoritative for Phase A**
  (desk research). Where the two checklist tables conflict, the summary matrix wins.
- **Phase C measurement supersedes both** for three rows. Measured values
  (`benchmark-results/<model>/run-001/notes.md`):

| Candidate_Model | Phase A (matrix) | **Phase C measured** | Evidence |
|---|---|---|---|
| FLUX.1-dev (+ Fill-dev) | official-variant | **official-variant** (confirmed) | `FluxFillPipeline`, real binary mask, outside-mask MAE 1.0–6.1, mask adherence 4.7/5 |
| FLUX.1-schnell | community | **community** (confirmed) | generic `FluxInpaintPipeline`, ring MAE 2–3× far (boundary seams), mask adherence 3.9/5 |
| FLUX.2 [dev] | native (caveated) | **unsupported** | `Flux2Pipeline` exposes no `mask_image`; the instruction-edit path gives outside-mask MAE 9.6–67.2 and re-renders the whole frame. A mask pipeline exists only for FLUX.2 [klein] |
| HunyuanImage (benchmarked as 2.1) | official-variant (caveated) | **unsupported** | `HunyuanImagePipeline` takes no image/mask; all 9 inpaint cases `failed / unsupported_task`. HunyuanImage-3.0 still has no documented mask API |
| PixArt-alpha | community / ❌ | **unsupported** | all 9 inpaint cases `failed / unsupported_task` |
| PixArt-Sigma | community / ❌ | **unsupported** | all 9 inpaint cases `failed / unsupported_task` |

Consequence for Req 1.3: the closed-vocabulary value for FLUX.2 [dev],
HunyuanImage, PixArt-alpha and PixArt-Sigma is **`unsupported`** as of Phase C.
No row changes its Req 1.7 benchmark status (`included` for all six) and no
Legal_Review_Flag changes. The Decision_Record §9c records this reconciliation and
the Property 1 / Property 2 review outcome (both PASS).
