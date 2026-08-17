# Integration Proposal — Selfhosted_Provider for the Synthetic Data Generator

**Task 8.2 deliverable** (_Requirements: 6.1, 6.2, 6.3, 6.4, 6.5, 6.6_).
Design proposal only — **no portal code, frontend, or CDK is changed by this
exploration** (Req 9.2). All code shapes below are proposals for a future
implementation spec.

Verified current pipeline (read 2026-08-17, unmodified):

- `synthetic_data.py` is one Lambda (1024 MB, **15 min timeout**) serving both the
  `/api/v1/synthetic` routes and the async generation worker, dispatched on
  `internal_action == 'generation_worker'` after a self-invoke with
  `InvocationType='Event'`; `POST …/generate` returns **202**.
- `run_generation_worker` builds one request per task via `_build_image_request`
  (Bedrock `taskType: INPAINTING` with a **text** `maskPrompt`, or
  `IMAGE_VARIATION`), calls `_invoke_image_model` → `bedrock:invoke_model`, writes
  the PNG to the use-case account's staging prefix, and records one preview item
  per task.
- `execute_generation_tasks(tasks, invoke_task, on_result)` gives **per-task failure
  isolation**: any exception from `invoke_task` becomes
  `preview['status']='failed'` + `preview['failure_reason']=str(exc)`, the preview
  is still persisted by `on_result`, `_record_last_failure` updates the session, and
  the loop continues. Completed/failed exactly partition the plan.
- Auto-annotation prefers `preview['mask_region']` (source `inpainting_mask`) and
  falls back to `bbox_from_diff` (source `image_diff`). **Today `mask_region` is
  never set** on the Bedrock path, because Nova Canvas inpainting is driven by a
  text `maskPrompt`, not a binary mask.

## 1. What Phase C says the integration must support

`benchmark-results/` evidence that constrains this design:

- **FLUX.1-Fill-dev is the only benchmarked model with true mask parity**
  (outside-mask MAE 1.0–6.1; ring ≈ far, i.e. no spill). It takes **source image +
  binary mask + prompt** — `FluxFillPipeline`.
- **FLUX.1-schnell** runs inpainting only through the generic community
  `FluxInpaintPipeline` (latent noising, strength 0.85): mask adherence 3.9/5 vs
  4.7/5, visible boundary seams (ring MAE 2–3× far on real photos).
- **FLUX.2 [dev] has no mask API** (`Flux2Pipeline` exposes no `mask_image`);
  outside-mask MAE 9.6–67.2. **PixArt-alpha/Sigma and HunyuanImage-2.1 are
  text-to-image only** (`unsupported_task` on all 9 inpaint cases).
- Measured latency: schnell 20.3 s, Fill-dev 36.7 s (768², offload mode),
  FLUX.2 81.5 s. Model load 4.2–4.4 s (FLUX) once weights are local.

Consequence: the adapter contract must carry a **binary mask**, which the pipeline
does not currently produce. That gap is §4 and is the single biggest open question
this proposal raises.

## 2. Selfhosted_Provider and adapter selection (Req 6.1, 6.2)

Generalizing the `stability-generation-models` Provider / Request_Adapter split:
that spec keys the adapter off the **model id** (Amazon vs Stability request
shapes). Here, adapter selection moves to the registry's **`provider_type`** plus
the transport `kind` (`model-registry-proposal.md` §3), so a new self-hosted model
needs a registry row, not a code change — unless it needs a genuinely new request
schema.

```
resolve_adapter(entry, env) -> Adapter
    entry.provider_type == "bedrock"     -> BedrockAdapter          (UNCHANGED code)
    entry.provider_type == "selfhosted"
        kind == "sagemaker-realtime"     -> SageMakerSyncAdapter
        kind == "sagemaker-async"        -> SageMakerAsyncAdapter
        kind == "https"                  -> HttpsAdapter
```

Adapter interface (mirrors what `run_generation_worker` already needs, so the
worker loop is unchanged in shape):

```python
class Adapter(Protocol):
    def build_request(self, task: GenerationTask, entry: RegistryEntry,
                      method: str) -> dict: ...
    def invoke(self, request: dict, endpoint: EndpointConfig) -> Invocation: ...
    #   Invocation = ImageResult(png_bytes) | Pending(inference_id, output_uri)
    def extract_image(self, response: Any) -> bytes: ...
```

- `BedrockAdapter` is **literally the existing code path** —
  `_build_image_request` + `_invoke_image_model` moved behind the interface with
  no change to the bytes they produce (§5, invariant 4).
- `SageMakerSyncAdapter` calls `sagemaker-runtime:InvokeEndpoint`
  (`ContentType: application/json`), SigV4 via the worker's execution role, IAM
  scoped to an endpoint-ARN prefix (`model-registry-proposal.md` §7).
- `SageMakerAsyncAdapter` calls `InvokeEndpointAsync` with the request JSON staged
  to S3, returning `Pending(inference_id, output_uri)` instead of image bytes —
  the only adapter that changes the worker's control flow (§6).
- `HttpsAdapter` POSTs to a VPC-internal ALB behind a Secrets Manager-held base
  URL/token; the Lambda must be VPC-attached. Documented as the fallback shape
  (`hosting-comparison.md` §5), not the recommendation.

Server side (proposed, not built): a single container per model serving
`POST /invocations` and `GET /ping` (SageMaker's contract), wrapping the exact
`diffusers` pipelines Phase C measured — `FluxFillPipeline` for Fill-dev,
`FluxInpaintPipeline`/`FluxPipeline` for schnell. The Phase C driver
(`benchmark-harness/run_driver.py`) is the reference implementation for load and
generation, including the `enable_model_cpu_offload` posture and the ≥64 GiB
host-RAM requirement.

## 3. Per-model adapter mappings (Req 6.3)

Common envelope for every self-hosted request (versioned so the container can
evolve):

```json
{
  "schema": 1,
  "task": "inpainting" | "text_to_image" | "image_variation",
  "prompt": "<resolved_prompt>",
  "seed": 123456789,
  "image_b64": "<source PNG base64>",
  "mask_b64": "<binary mask PNG base64, 255=edit region>",
  "params": {"guidance_scale": 30.0, "num_inference_steps": 50,
             "width": 768, "height": 768, "strength": 0.85}
}
```

Response envelope: `{"schema": 1, "images": ["<base64 PNG>"], "seed_used": N,
"model": "...", "latency_ms": N}` — deliberately the same `images[0]` shape the
existing `_invoke_image_model` already parses, so `extract_image` is shared.

### 3a. FLUX.1-Fill-dev — inpainting (primary path, `official-variant`)

| Pipeline input (task → request) | Field | Value / derivation |
|---|---|---|
| source image | `image_b64` | staging/source object bytes, base64 (as today) |
| **mask** | `mask_b64` | binary mask PNG, 255 inside the defect region (see §4) |
| resolved prompt | `prompt` | `task['resolved_prompt']` verbatim — no rewriting |
| Task_Seed | `seed` | `task['seed']` from `derive_task_seed`, passed through unchanged |
| cfg_scale | `params.guidance_scale` | `params['cfg_scale']` or `randomization_defaults.cfg_scale`; Fill-dev's working value in Phase C was 30.0 (FLUX guidance domain, **not** Nova Canvas's 6.5 — the registry default per entry is what makes this correct) |
| steps | `params.num_inference_steps` | 50 (Phase C measured shape) |
| resolution | `params.width/height` | 768 (Phase C measured; registry `max_resolution`) |
| — | `params.strength` | not used by FluxFillPipeline |

| Response → pipeline | Derivation |
|---|---|
| image bytes | `base64decode(images[0])` → `s3:put_object` to the existing staging key |
| `mask_region` | `bbox_from_mask(mask)` computed from the same mask that was sent → preview `mask_region`, annotation source `inpainting_mask` |
| `seed` / `resolved_prompt` / `model_id` | already written by `execute_generation_tasks`/plan; unchanged |

Server: `FluxFillPipeline.from_pretrained("black-forest-labs/FLUX.1-Fill-dev",
torch_dtype=bfloat16)` + `enable_model_cpu_offload()`; call with
`image`, `mask_image`, `prompt`, `guidance_scale`, `num_inference_steps`,
`generator=torch.Generator("cpu").manual_seed(seed)`.

### 3b. FLUX.1-schnell — inpainting (`community`) and text-to-image

| Field | Value / derivation |
|---|---|
| `image_b64`, `mask_b64`, `prompt`, `seed` | as above |
| `params.num_inference_steps` | **4** (timestep-distilled) |
| `params.strength` | 0.85 (Phase C shape; the community pipeline's mask behaviour depends on it) |
| `params.guidance_scale` | **omitted** — schnell ignores guidance; the registry entry sets `capabilities.cfg_scale = false` so the worker never sends it (exactly the existing conditional in `_build_image_request`) |

Response mapping identical to 3a. Quality caveat recorded on the registry entry
(`inpainting_path: "community"`, `notes`) so the admin UI can warn: measured mask
adherence 3.9/5 with boundary seams.

### 3c. Text-to-image-only models (PixArt-alpha / Sigma, HunyuanImage-2.1)

Included for completeness; **not recommended** for the primary path.

| Field | Value |
|---|---|
| `task` | `text_to_image` |
| `image_b64` / `mask_b64` | absent |
| `params.num_inference_steps` | PixArt 20, Hunyuan 50 (Phase C) |
| `params.width/height` | 1024 |

Response: image bytes only. **No `mask_region`** → auto-annotation falls back to
`bbox_from_diff`, which is meaningless without a source image, so these models can
only serve a "generate defect exemplar from scratch" flow, not the pipeline's
mask-constrained path. An `inpainting` task sent to such an entry must be rejected
**before** invocation using the registry's capability flags, surfacing
`unsupported_task` (the same failure mode Phase C recorded) rather than a wasted
GPU call.

### 3d. FLUX.2 [dev]

No mapping is proposed. `Flux2Pipeline` accepts no mask, and the instruction-edit
workaround measured outside-mask MAE 9.6–67.2 — it re-renders the whole frame, so
`bbox_from_mask` cannot be driven from it and background pixels are not preserved.
If FLUX.2 is ever revisited, the candidate is **FLUX.2 [klein]** (Apache 2.0),
which does have `Flux2KleinInpaintPipeline`; it was never benchmarked.

## 4. The mask gap (must be resolved by the implementation spec)

The pipeline has **no binary mask today**. Nova Canvas inpainting is driven by
`maskPrompt` text (`"the {defect_type} region on the {object_type}"`), and no
`mask_region` is recorded, so annotation uses `image_diff`. FLUX.1-Fill-dev needs
an actual mask raster.

Options, with recommendation:

| Option | Description | Assessment |
|---|---|---|
| **A. User-drawn region (recommended)** | The session UI gains an optional rectangle/brush over each source image; the backend rasterizes it to a binary PNG per source | Deterministic, cheap, and it *improves* annotation quality: `mask_region` becomes exact and the annotation source upgrades from `image_diff` to `inpainting_mask`. Requires frontend work in the implementation spec |
| B. Text-prompt segmentation | Keep `maskPrompt` and derive a mask with a segmentation model (e.g. Grounding-DINO/SAM) on the same endpoint | Preserves the current UX but adds a second model, more latency, and a new failure mode; the mask becomes non-deterministic across runs, weakening seed reproducibility |
| C. Whole-image mask | Send an all-255 mask | Defeats the purpose — mask parity was the whole reason Fill-dev won; equivalent to img2img |
| D. Per-source uploaded mask | Accept a mask PNG alongside each source image | Simplest backend, worst UX; useful as a power-user/API path |

**Recommendation: A, with D as an API-level escape hatch.** Both keep the mask
deterministic per source image, so the Task_Seed determinism invariant continues to
imply reproducible output for a given (source, mask, prompt, seed).
If neither is implemented, self-hosted FLUX inpainting cannot be wired up at all —
this is a hard prerequisite, recorded in `decision-record.md`.

## 5. Pipeline_Invariants (Req 6.4)

### Invariant 1 — Task_Seed determinism via unchanged `derive_task_seed`

`derive_task_seed(base_seed, task_index) = (base_seed + task_index) % 858_993_460`
stays **untouched**, and `build_generation_plan` keeps assigning seeds. The seed
flows into `request.seed` and then into `torch.Generator(...).manual_seed(seed)`.

- **Seed-domain note:** `SEED_MODULUS = 858_993_460` exists because Nova Canvas
  accepts `0..858_993_459`. Torch generators accept the full 64-bit range, so the
  Nova-derived modulus is *narrower* than any self-hosted model requires — it
  remains valid and must **not** be widened (widening would change existing Bedrock
  seeds and break reproducibility of already-generated sessions).
- Same seed + same mask + same prompt + same model + same steps/guidance ⇒ same
  image on a fixed pipeline version. Cross-version reproducibility is not claimed
  (diffusers/torch upgrades can shift outputs); the registry's `notes` should pin
  the container image digest per entry so a session's provenance is exact.

### Invariant 2 — Per-preview metadata (model id, seed, resolved prompt)

Unchanged and automatic: `execute_generation_tasks` writes `resolved_prompt`,
`seed`, `source_image_key`, `variation_index`, `approval_state` for every task
before invocation, and the plan carries `model_id` per task. The self-hosted
adapter returns only the *extra* fields (`preview_id`, `staging_key`,
`generation_method`, `mask_region`) exactly as `invoke_task` does today. **No
preview field is renamed, removed, or made conditional.** Recommended additions
(purely additive, so existing readers keep working): `provider_type`,
`endpoint_name`, `container_image_digest`, `latency_ms`.

### Invariant 3 — Mask_Region recording for `bbox_from_mask` auto-annotation

For every self-hosted inpainting task the adapter sets
`preview['mask_region'] = bbox_from_mask(mask)` from the **same mask bytes it
sent**, so `_resolve_bbox` takes the existing `mask_region` branch and reports
annotation source `inpainting_mask`. `bbox_from_mask` and `bbox_from_diff` are not
modified. This is a strict improvement over today's Bedrock path, which has no
`mask_region` and therefore always uses `image_diff`.

### Invariant 4 — Byte-identical Nova Canvas behaviour

`provider_type: "bedrock"` routes to the untouched `_build_image_request` /
`_invoke_image_model` pair: same `taskType`, same `inPaintingParams.maskPrompt`
text built from the same f-string, same `imageGenerationConfig` (including the
capability-flag conditionals for `seed`/`cfgScale`), same `numberOfImages: 1`,
same `modelId`. The refactor is a *dispatch* change, not a request change.

**How to prove it in the implementation spec:** a preservation property test that
serializes `_build_image_request(...)` for a generated matrix of
(method, prompt, seed, params, mask_prompt) inputs and asserts byte equality
between pre- and post-refactor code paths. The portal already has this pattern —
`edge-cv-portal/backend/tests/test_property_bedrock_sampling_preservation.py` and
the stability spec's `test_property_amazon_request_preservation.py` — so the
implementation should extend, not invent, it.

## 6. Error taxonomy (Req 6.5)

Four codes, raised as typed exceptions from the adapter, each carrying a stable
machine code plus a human message. `execute_generation_tasks` already converts any
exception into `preview['status']='failed'` + `preview['failure_reason']` and calls
`_record_last_failure` on the session, so **no new failure-recording mechanism is
needed** — only stable strings.

| Code | Raised when | Mapped from | Recorded as | Retryable |
|---|---|---|---|---|
| `endpoint_unreachable` | `ValidationError`/`ResourceNotFound` from SageMaker for an unknown or deleted endpoint; DNS/connect failure or 5xx from an `https` endpoint; `DescribeEndpoint` status `Failed`/`OutOfService` | botocore `ClientError`, `EndpointConnectionError`, HTTP 502/503/504 | `failure_reason: "endpoint_unreachable: <detail>"` | no (config problem) — fail the remaining tasks fast rather than burning 20 timeouts |
| `endpoint_cold_starting` | on-demand entry whose endpoint has zero instances / is `Updating`, or an async submission accepted but not yet complete when the worker's budget expires | `DescribeEndpoint` + instance count, or an async `Pending` that outlives the budget | `failure_reason: "endpoint_cold_starting: retry in ~Ns"`, preview left `failed` **only if** the resume path (§7) is unavailable | **yes** — the one code the implementation should retry automatically |
| `generation_failure` | endpoint returned 200 but the model errored (CUDA OOM, unsupported task, safety refusal, `images: []`) | response `error` field or empty `images` — the same condition `_invoke_image_model` already raises `RuntimeError` for | `failure_reason: "generation_failure: <model detail>"` | no (per-task); the loop continues to the next task |
| `malformed_response` | 200 with unparsable JSON, missing `images`, or non-decodable base64 | JSON/base64 decode errors, schema mismatch | `failure_reason: "malformed_response: <detail>"` | no — indicates a container/version mismatch; surfaces the pinned image digest |

Additional rules:

- **`unsupported_task` is pre-flight, not a failure of the endpoint:** if the
  registry's capability flags say the model cannot do the selected method, the task
  fails with `generation_failure: unsupported_task` **before** any invocation
  (mirrors the Phase C recording and wastes no GPU time).
- **Fail-fast escalation:** on the *first* `endpoint_unreachable`, mark the
  remaining tasks failed with the same code instead of retrying each — otherwise a
  20-variation plan burns the whole 15-minute Lambda budget on connection errors.
  `completed ∪ failed` must still exactly partition the plan (the existing
  invariant of `execute_generation_tasks`).
- **Session-level reporting** stays `_record_last_failure`, so the existing UI
  surfaces the reason with no change.

## 7. Lambda timeout handling and the async pattern (Req 6.6)

**Measured budget arithmetic** against the worker's 900 s timeout:

| Model | Measured s/image | 20-variation plan | % of 900 s budget | Verdict |
|---|---|---|---|---|
| FLUX.1-schnell | 20.3 | 406 s | 45 % | comfortable |
| FLUX.1-Fill-dev | 36.7 (first case 40.4) | 734–808 s | 82–90 % | **fits, no margin for a cold start** |
| FLUX.2 [dev] | 81.5 | 1,630 s | 181 % | exceeds the budget |
| HunyuanImage-2.1 | 39.7 | 794 s | 88 % | fits, no margin |
| PixArt | 6.7–7.0 | 134–140 s | 16 % | comfortable |

Also binding: **`InvokeEndpoint` has a 60 s per-invocation ceiling.** Fill-dev's
36.7 s fits; FLUX.2's 81.5 s does not (hence no real-time row in the cost model).

Three patterns, chosen by `availability_mode`:

1. **Always-on + synchronous (recommended for `prod`).** Keep today's loop
   verbatim, swapping `bedrock:invoke_model` for
   `sagemaker-runtime:InvokeEndpoint`. Add a **budget guard**: before each task,
   if `context.get_remaining_time_in_millis()` < (measured p95 latency + 30 s
   margin), stop, persist the completed previews, and **self-invoke the worker to
   resume** the remaining tasks. The existing `generation_pass` conditional makes
   resumption safe — a stale worker cannot overwrite a newer pass — and previews
   are already written incrementally by `on_result`, so nothing is lost or
   duplicated. This also removes the current implicit 20-variation ceiling.
2. **On-demand + async (recommended for `dev`).** `SageMakerAsyncAdapter` submits
   every task with `InvokeEndpointAsync` (payload staged to S3), persists
   `{preview_id, inference_id, output_location}` on each preview with
   `status: 'in_progress'`, and returns immediately — **the worker never waits out
   the 4–10 min cold start**, so a scale-from-zero event cannot consume the Lambda
   budget. Completion is handled by a small **resume handler** triggered by the
   async endpoint's success/error SNS notifications (or an S3 event on the output
   prefix): it reads the output JSON, writes the PNG to staging, and flips the
   preview to `completed`/`failed` using the same `on_result` write path plus the
   `generation_pass` guard. A time-based sweeper (EventBridge, every 5 min)
   reconciles previews stuck `in_progress` beyond
   `expected_cold_start_seconds + p95 × queue_depth`, marking them
   `endpoint_cold_starting` for retry.
   - Preview status vocabulary gains `in_progress`. It is additive: existing
     readers treat unknown-but-not-`completed` as not-yet-approvable, and
     `select_approved` filters on `approval_state`, which is untouched.
3. **Polling fallback** (if SNS/S3-event wiring is undesirable): the worker
   self-invokes itself on a delay to poll async results. Simpler IAM, more
   invocations; acceptable but strictly worse than 2.

**Bedrock path unaffected:** Nova Canvas keeps the synchronous loop with no budget
guard change required (its latency is seconds, not tens of seconds), preserving
invariant 4.

## 8. Requirement coverage

| AC | Where satisfied |
|---|---|
| 6.1 Integration_Proposal exists as a design document | this document |
| 6.2 Selfhosted_Provider whose Request_Adapter invokes SageMaker/HTTP, generalizing the stability Provider/Request_Adapter split | §2 |
| 6.3 per-recommended-model adapter mapping (inputs → request schema; response → image bytes) | §3a–3d + the shared envelope in §3 |
| 6.4 every Pipeline_Invariant preserved (seed, per-preview metadata, Mask_Region, byte-identical Nova Canvas) | §5 invariants 1–4, with the preservation-test recipe |
| 6.5 error taxonomy mapped to existing per-task failure recording | §6 |
| 6.6 Lambda timeout constraint + async invocation pattern for on-demand | §7 |

## 9. Open questions for the implementation spec

1. **Mask acquisition (§4)** — hard prerequisite. Without a binary mask there is no
   self-hosted inpainting path at all.
2. **Container ownership** — who builds and pins the diffusers serving image, and
   where its digest is recorded (proposed: registry `notes` + preview provenance).
3. **Weights distribution** — FLUX repos are ~55–58 GB and gated on Hugging Face
   (licence acceptance). A production deployment needs an internal mirror (S3) plus
   a documented licence-acceptance trail; the exploration's temporary HF token was
   deleted with the rest of the benchmark infrastructure.
4. **Resolution parity** — Phase C measured inpainting at 768²; the portal's
   annotation defaults assume 1024². Confirm the served resolution and the
   resulting `image_size` recorded in manifest records.
5. **Unmeasured SageMaker cold start** — every async-mode timing number here is an
   estimate (`hosting-comparison.md` §1); measure it with one short-lived endpoint
   before committing to the `dev` shape (quota is available: `ml.g6e.2xlarge` = 4).
