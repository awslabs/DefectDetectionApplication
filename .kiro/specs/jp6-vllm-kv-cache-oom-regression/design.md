# JP6 vLLM KV-Cache OOM Regression Bugfix Design

## Overview

`qwen2-5-vl-7b-instruct-awq` cannot load on `LocalServer.arm64JP6` **1.0.61**
while the identical model with the identical staged engine args loads on
**1.0.59**, and the publish-time gate that let the configuration ship reports
4.50 GiB of slack for a load whose device-measured KV remainder is
**−7.83 GiB**. This design fixes both halves: the version-to-version regression
(an unbudgeted device-side multimodal default) and the unsound sizing model that
made the configuration look feasible.

**Posture (decided; binding on every decision below).** The fix REDUCES vLLM's
demand and makes the sizing model SOUND. It does **not** buy KV headroom by
raising `gpu_memory_utilization`, and it does **not** relocate the three
co-resident ONNX GPU models off JP6 devices. Both halves of the success
condition are binding and are treated as a conjunction throughout: the published
vLLM model loads and serves on JP6 **AND**
`model-cookies-binary-jetson-xavier-jp6`,
`model-rf-detr-seg-nano-jetson-xavier-jp6` and
`model-yolo-test-jetson-xavier-jp6` keep serving on GPU unchanged. A change that
satisfies one half at the other's expense is a failure, not a fix.

Three consequences of that posture shape the whole design:

1. **The device leg alone restores service.** The staged, already-published
   `model.json` omits `limit_mm_per_prompt`. Removing the runtime's
   unconditional `{"image": 2}` default returns 1.0.61's profiling demand to
   1.0.59's, so the currently-broken model loads again **without re-packaging or
   re-publishing anything**. The portal leg prevents recurrence and makes the
   two-image capability an authored, sized decision.
2. **The sizing model must get more conservative, and it will refuse
   configurations that publish today.** That is the intended direction. It also
   means the corrected model must be able to say something true and useful about
   a configuration that *does* serve but with 0.65 GiB of KV — hence the
   thin-margin warning rather than a binary verdict alone.
3. **Publish-time math can only ever be a necessary condition.** The non-torch
   term vLLM charges against its own budget swung 8.34 GiB between two attempts
   four minutes apart on the same device. No authoring-time formula can predict
   that. The design therefore pairs the corrected publish-time gate with a cheap
   device-side preflight that reads *actual* memory, and says plainly that the
   former is necessary-not-sufficient.

This spec **revises** the sizing model owned by the sibling spec
`vllm-sizing-and-packaging-errors` (Decision 2 enumerates every sibling
requirement and test that must be consciously repointed, recorded verbatim
first). It does not touch the reconciler
(`vllm-model-reload-after-backend-restart`), the runtime server's routes and
status maps, the JP7 image, or the GPU-fallback status surfaces owned by
`model-gpu-fallback-visibility`.

## Glossary

- **Bug_Condition (C)**: A publish-and-deploy attempt for a vLLM model where the
  shipped pipeline reports the configuration as feasible (or applies an
  unbudgeted device-side default) while the device provably cannot load it
  inside the configured budget.
- **Property (P)**: For inputs satisfying C, the fixed pipeline refuses the
  configuration (or the load) with a verdict that names the terms and offers
  demand-reducing remediation — before spending ~4 min of profiling and before
  taking a Greengrass deployment down.
- **Preservation**: The verdict, response shape, staged `model.json`, prep
  lifecycle semantics, reconciler behavior, JP7 load behavior and the three JP6
  ONNX models for every input NOT satisfying C.
- **Fit_Check**: `edge-cv-portal/backend/functions/vllm_fit_check.py` — the
  publish/registration-time sizing verdict.
- **Budget**: `gpu_memory_utilization × DEVICE_MEMORY_PROFILE_BYTES[arch]` — the
  memory vLLM will target. `gpu_memory_utilization` is a fraction of **total**
  device memory, not of free memory.
- **Activation_Allowance**: The estimated PyTorch activation/profiling peak vLLM
  charges against the Budget. It scales with **Multimodal_Units**, not with images
  alone (amended 2026-08-19). Measured points, all same model: 4.92 GiB at
  `util = 0.4` on 1.0.59 (one image, video **unbound**); on 1.0.62 at
  `util = 0.55`, **2.47 GiB** with `{'image': 1, 'video': 0}` and **4.93 GiB** with
  `{'image': 1}` (video unbound).
- **Multimodal_Units**: The total of the authored per-modality limits
  (`limit_mm_per_prompt.image + limit_mm_per_prompt.video`) the
  Activation_Allowance is sized for. vLLM reserves its worst-case multimodal token
  budget **per modality** (its own warning: 32768 tokens,
  `{'image': 16384, 'video': 16384}`), so an **absent** `video` key is priced at
  vLLM's own default of 1 — a full extra unit — while an authored `"video": 0`
  costs nothing. This is why the product's default is `{'image': 1, 'video': 0}`:
  one unit.
- **Co_Tenancy_Reservation**: Per-architecture memory held by other consumers of
  the same unified memory before vLLM starts (measured ≈5.7 GiB in the three
  ONNX Triton python-backend stubs plus the containers; `free -g` showed 6 GB
  used at a clean backend restart).
- **Fraction_Cap**: `(profile[arch] − Co_Tenancy_Reservation) / profile[arch]` —
  the largest `gpu_memory_utilization` that does not, by construction, claim
  memory the co-tenants already hold. JP6: `(30 − 6)/30 = 0.80`.
- **Device_Preflight**: The new device-side check that reads actual available
  memory (from `/proc/meminfo`, never from CUDA) and the staged args, and refuses
  a doomed load in seconds instead of ~4 min.
- **Starvation_Latch**: Per-backend-life record that a previous failed attempt's
  memory did not come back, used to refuse retrying into a starved device.
- **Thin_Margin**: A load that reaches READY with a KV remainder below
  `MINIMUM_KV_CACHE_BYTES` (observed 0.65 GiB against a 1 GiB floor).

## Bug Details

### Bug Condition

The publish-time gate passes configurations the device cannot load because its
required-bytes term omits the activation/profiling peak entirely, and its budget
term is a fraction of TOTAL memory on a device where other consumers already
hold ~6 GB; the device then applies an unbudgeted multimodal default that
enlarges the very term the gate omitted, and has no cheap way to discover any of
this before spending ~4 min of profiling on a doomed load.

**Formal Specification:**

```
FUNCTION isBugCondition(X)
  INPUT: X of type LoadAttempt (bugfix.md record: arch, weights_bytes, util,
         max_model_len, mm_images_per_prompt, device_total_bytes,
         co_resident_bytes, activation_peak_bytes, prior_failed_attempt)
  OUTPUT: boolean

  budget   := X.util * profile[X.arch]
  claimed  := X.weights_bytes + MINIMUM_KV_CACHE            // shipped model
  actual   := X.weights_bytes
              + activation_peak(X.weights_bytes, X.mm_images_per_prompt)
              + charged_non_torch(X.co_resident_bytes, X.prior_failed_attempt)
              + MINIMUM_KV_CACHE

  // C1  the gate passes a configuration the device cannot load
  C1 := (budget >= claimed) AND (budget < actual)
  // C2  remediation grows the claim into memory co-tenants hold
  C2 := X.util_raised AND (budget + X.co_resident_bytes > X.device_total_bytes)
  // C3  the multimodal default enlarges an already-published model's peak
  C3 := staged_model_json_omits(limit_mm_per_prompt)
        AND runtime_forces(mm_images_per_prompt = 2)
  // C4  a failed attempt stranded allocations
  C4 := X.prior_failed_attempt AND NOT reclaimed(previous_attempt)
  // C5  per-arch infeasibility ships because another arch fits
  C5 := (NOT fits(X.arch)) AND (EXISTS a != X.arch : fits(a))
  // C6  a load reached READY below the KV floor and reported success
  C6 := load_ready(X) AND (kv_remainder(X) < MINIMUM_KV_CACHE)

  RETURN C1 OR C2 OR C3 OR C4 OR C5 OR C6
END FUNCTION
```

### Examples

- **C1, the incident** (`qwen2-5-vl-7b-instruct-awq`, `arm64_jp6`, `util=0.4`):
  gate says `0.4 × 30 GiB = 12.00 GiB ≥ 6.5 + 1 = 7.5 GiB` — PASSES with a
  claimed 4.50 GiB of slack. Device: `model weights take 6.59GiB; non_torch
  8.29GiB; activation peak 4.93GiB; the rest reserved for KV Cache is -7.83GiB`
  → HTTP 409 `No available memory for the cache blocks`. Expected: the verdict
  is FAILS and names the ~4.9 GiB activation term.
- **C2, the hazard**: the same two surfaces that reported the failure tell the
  operator to RAISE `gpu_memory_utilization`. At `util=0.9` on JP6 the budget is
  27 GiB while co-tenants hold ~6 GiB of a 30 GiB device — 33 GiB of claims on a
  29.95 GiB device. Expected: raising the fraction is offered only below the
  Fraction_Cap (0.80 on JP6), after the demand-reducing remediations, and never
  without the co-tenancy hazard stated.
- **C3, the regression**: 1.0.61's `manager.load` does
  `engine_args.setdefault("limit_mm_per_prompt", {"image": 2})`; the staged
  `model.json` omits the key (confirmed: `grep -c limit_mm_per_prompt
  /vllm_runtime/manager.py` → 0 in the running 1.0.59 container). Two images are
  profiled where one was, inside an unchanged 11.98 GiB budget whose one-image
  activation peak is already 4.92 GiB. Expected: the effective limit is authored
  and sized, or it stays at vLLM's own default.
- **C4, the cascade**: three failed loads left the device at **26 GB used /
  3 GB free with no model loaded**; `_reclaim_gpu_memory` cleared an 8.34 GiB
  non-torch swing on the KV-OOM path but not across the NVML-assert path; only a
  container restart recovered. Expected: reclaim covers the path, or the retry is
  refused with a diagnostic.
- **C5, the escape**: `greengrass_publish.py` blocks only when
  `every_arch_fails = all(not finding.fits …)`, so an `arm64_jp6`-infeasible
  configuration publishes because `arm64_jp7` fits. Expected: the per-arch
  verdict gates the arch it applies to.
- **C6, the thin margin**: the retry reached READY with `the rest of the memory
  reserved for KV Cache is 0.65GiB` against a 1 GiB floor, reported as an
  unqualified READY. Expected: a WARNING naming the margin.
- **Edge case (not the bug)**: a model whose weights alone exceed the budget —
  `vllm-sizing-and-packaging-errors`' original incident. Its verdict stays FAILS
  and "raise the fraction or shrink the model" remains correct *for that
  arithmetic*; it is now stated with the co-tenancy cap attached.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**

- **JP7 device behavior (3.4)**: `qwen3-vl-8b-instruct` keeps loading on
  `LocalServer.arm64JP7` with `Available KV cache memory: 36.34 GiB` /
  `GPU KV cache size: 264,592 tokens` under `gpu_memory_utilization=0.5`, three
  vision models co-resident. No JP7 image, engine default, or code path is
  touched by this design.
- **The three JP6 ONNX GPU models (3.6)**: unchanged load-to-READY on GPU,
  unchanged inference behavior, unchanged footprint. The design never enlarges
  vLLM's claim; the Fraction_Cap exists specifically to protect them.
- **Reconciler (3.7)**: `reconciler.py` and its wiring in `app.py` are not
  touched — one-shot scan, sequential re-drive, `(30, 120, 480)` backoff,
  tombstone semantics, and the `no staged models awaiting reload; nothing to do`
  line all stand. (The preflight makes a re-driven doomed load cost seconds
  instead of ~4 min, which changes timing, not semantics.)
- **Prep lifecycle semantics (3.8)**: atomic staging; `LOAD_UNREACHABLE` /
  `LOAD_HTTP_ERROR` → exit 1 with the authoritative log; the single KV-OOM
  unload→reload recovery per attempt for genuine KV-OOM; the prominent ERROR line
  carrying model name, HTTP status, extracted reason and the staged
  `gpu_memory_utilization` / `max_model_len`; idempotent Shutdown/`--cleanup`.
- **Two-image reference generation (3.9)**: `vlm-anomaly-reference-parity`
  Requirement 6.6 keeps working for models sized for it. The capability is not
  removed; it becomes authored and budgeted.
- **Registration/update never blocks (3.2)**: an undeterminable Weight_Estimate
  still yields `unverified`; `estimate_weights` stays stdlib-only and never
  raises out of its public API.
- **Publish response shape (3.1)**: `passed` / `warnings` / `overridden` /
  `unverified` statuses, the `fit_check` annotation, and the audit events keep
  their shape; `skip_fit_check` keeps working and keeps being audited.
- **Engine_Configuration contract (3.3)**: the five existing settings keep their
  keys, defaults and validated ranges; unknown keys stay fail-closed with
  per-field findings; stored values still propagate verbatim into `model.json`.
- **Vision/ONNX packaging and publishing (3.10)**, **JP5/x86 vLLM-free inertness
  (3.5)**, and the currently-serving 1.0.59 device (3.11) are untouched until a
  fixed component is deliberately deployed.

**Scope:** every input where none of C1–C6 holds must be byte-identical:
non-multimodal models, models that genuinely fit, records with an undeterminable
estimate, JP7 records, JP5/x86 images, every ONNX/Triton path, and every load
that reaches READY with a healthy margin.

## Hypothesized Root Cause

1. **Unbudgeted device-side multimodal default (leading cause of the
   version-to-version regression)**. Commit `086c251` added
   `engine_args.setdefault("limit_mm_per_prompt", {"image": 2})` to
   `manager.load`. The staged args omit the key, so 1.0.61 profiles a
   vision-language engine for two images where 1.0.59 profiled for one, inside an
   unchanged 11.98 GiB budget whose one-image activation peak was already
   4.92 GiB (41% of the budget) leaving 0.65 GiB of KV. Growing that term is
   sufficient to drive the remainder negative. **The 4.92 GiB figure is the
   ONE-image number measured on 1.0.59; the two-image peak is unmeasured
   [HARDWARE]** — this is a hypothesis about magnitude, not a measurement, and
   the design does not depend on the magnitude being exactly 2×.
2. **The sizing model omits two of the four terms vLLM charges**. `required =
   weights + 1 GiB` has no activation term at all, and the budget is a fraction
   of TOTAL memory with no notion of co-tenancy. The 1 GiB floor is NOT the main
   error (0.65 GiB served this model at 2.95x concurrency for 4096 tokens); the
   missing ~4.9 GiB activation term is.
3. **The profile entry is documented as "usable" but is a TOTAL figure**. The
   four device-reported terms sum to ≈29.95 GiB, so 30 GiB is a fair TOTAL for
   the 32 GB Orin AGX and a wrong "usable" (~6 GB is resident before vLLM
   starts).
4. **Remediation direction is inverted for this failure mode**. Both surfaces say
   RAISE the fraction; on shared unified memory that grows this model's claim on
   memory the ONNX models hold.
5. **No device-side truth check exists**. Engine args are decided at authoring
   time and consumed verbatim (`ENGINE_DEFAULTS` → `model.json` → prep →
   `repository.py` → `AsyncEngineArgs(**engine_args)`); nothing reads free memory,
   so the only failure signal is a ~4 min profiling run that blocks the runtime
   server's event loop and then fails the deployment.
6. **Reclaim does not cover the NVML-assert path — code-grounded hypothesis**.
   `_reclaim_gpu_memory` gates on `torch.cuda.is_initialized()` (deliberately, to
   avoid poisoning forked children). If the attempt dies inside the caching
   allocator's NVML query before/without a usable initialized CUDA state in this
   process, `empty_cache()` is skipped and nothing is returned to the driver —
   which matches "reclaim works on the KV-OOM path, not the assert path". The
   **root cause of the NVML assert itself stays an open question** (same-exhaustion
   symptom vs distinct CUDA/NVML fault) with a determination path recorded in
   Decision 6; this design reports it distinguishably rather than inventing a
   cause.

## Decisions

### Decision 1: the multimodal limit becomes an authored, sized engine setting; the device stops defaulting it

**Decision (REVISED 2026-08-19 — see "Schema revision" below; the original
single-key form is recorded verbatim there and is SUPERSEDED).**
`limit_mm_per_prompt` becomes a first-class `ENGINE_DEFAULTS` field with default
`{"image": 1, "video": 0}`, validated as an object whose keys are a **non-empty
subset of `{image, video}`** — `image` an integer **1..8**, `video` an integer
**0..8**, every other sub-key rejected fail-closed with its own per-field
finding — stored on the record, propagated **verbatim** into `model.json` (it is
already a standard `EngineArgs` field, so no translation is needed anywhere), and
read by the Fit_Check as the multimodal term of the Activation_Allowance. That
term is the **total of the authored per-modality counts** (images + videos), not
the image count alone, and an **absent** `video` key is priced at vLLM's own
per-modality default of **1** — so omitting the bound is deliberately more
expensive than authoring `"video": 0`. The device-side
`engine_args.setdefault("limit_mm_per_prompt", {"image": 2})` is **removed**: when
the staged args omit the key, the engine uses vLLM's own defaults and the demand
equals 1.0.59's.

**Schema revision (2026-08-19): per-modality keys, `video` bounded by default.**

*Recorded verbatim, the schema this SUPERSEDES:* "`limit_mm_per_prompt` becomes a
first-class `ENGINE_DEFAULTS` field with default `{"image": 1}`, validated
(object with the single key `image`, integer 1–8)".

*Why it is superseded:* the single-key schema **could not express the only
configuration measured to serve this model on JP6 with headroom.** Measured on
`ryanorinagxdevkithomelabjp622` (LocalServer.arm64JP6 **1.0.62**, MemTotal
**29.96 GiB**), both runs at `gpu_memory_utilization = 0.55`, same model, verbatim
from vLLM's own profiling output:

- `{'image': 1, 'video': 0}` → `model weights take 6.59GiB; non_torch_memory
  takes 0.98GiB; PyTorch activation peak memory takes 2.47GiB; the rest of the
  memory reserved for KV Cache is 6.43GiB`, `Maximum concurrency for 4096 tokens
  per request: 29.41x` — **READY** at 2026-08-19T00:29:52Z.
- `{'image': 1}` (video unbound) → `model weights take 6.59GiB;
  non_torch_memory takes 4.76GiB; PyTorch activation peak memory takes 4.93GiB;
  the rest of the memory reserved for KV Cache is 0.20GiB`, `Maximum concurrency
  ... 0.89x` — **FAILED**: `kv-cache-exhaustion: The model's max seq len (4096)
  is larger than the maximum number of tokens that can be stored in KV cache
  (3664)`.

vLLM's own warning explains the mechanism: `worst-case total number of
multimodal tokens (32768) ... out of which {'image': 16384, 'video': 16384} are
reserved for multi-modal embeddings`. **Half** the worst case is video this
product never sends — the inputs are camera frames and folder images — and the
engine sizes its activation peak from the limits it is given, not from the
traffic it receives. Bounding video to 0 **halves the activation peak, 4.93 →
2.47 GiB**, which is the attributable, repeatable effect of the bound.

*Honest note on the same two runs:* `non_torch_memory` **also** differed (0.98 vs
4.76 GiB), and that term is known to swing independently of configuration on this
device (historically −0.05 to 8.29 GiB, defect 1.7). The KV difference between the
two runs is therefore **not** wholly attributable to the video bound; the
**activation halving is**, and it is the only part this schema claims. The
end-to-end READY-vs-FAILED outcome of these two specific runs is a single
observation each, not a proven distribution.

*Consequences carried into the rest of this design:*

- The multimodal term of the Activation_Allowance counts **units** (images +
  videos), so an unauthored `video` costs a full extra unit — the refusing
  direction (Decision 2's formula, restated there).
- `image` keeps `1..8` (a vision-language model that accepts no image is not a
  configuration this portal authors) while `video` starts at **0** (a video-less
  model is exactly what this product ships). The two sub-keys deliberately have
  **different** ranges.
- Every other sub-key (`audio`, …) stays **fail-closed** with a per-field
  finding, and `{}` is rejected: a limit that bounds nothing is not a
  configuration.
- The engine-spec endpoint advertises both keys and both ranges, since both
  frontend forms are schema-driven off it — an unadvertised key is unauthorable.
- Preservation 3.9 is untouched: two-image reference generation is still
  authorable as `{"image": 2, "video": 0}`.
- **[ESTIMATE, not measured]** No claim is made here about `video: N > 0`
  configurations; the schema admits `0..8` for symmetry with `image`, but no
  video-serving configuration has been profiled on this hardware.
- **Records stored before this field existed are priced at two units.** Both
  Fit_Check call sites (`model_import.evaluate_fit_check`,
  `greengrass_publish`) pass the **stored** `engine_configuration`, not the
  resolved one, so a legacy record with no `limit_mm_per_prompt` gets vLLM's own
  defaults (1 image + 1 unbounded video = 2 units) and a correspondingly larger
  allowance. That is the honest reading of what such a record will actually do on
  a device, and it is the fail-closed direction; re-authoring the record with
  `{'image': 1, 'video': 0}` is the one-field fix and the remediation text says
  so.

**When a workflow needs two images but the loaded model was authored for one:
fail the request truthfully.** `_build_multimodal_prompt` rejects a
reference-image request before the engine is invoked, with a `GenerationError`
naming the model, the effective authored limit, and the remediation ("set
`limit_mm_per_prompt.image = 2` in the model's engine configuration, then
re-package and re-publish"). It does not silently drop the reference image.

**Rationale.**

- It puts the memory-relevant knob where the budget is computed. An unbudgeted
  device-side default is invisible to every sizing surface by construction —
  that is precisely defect 1.4 and expected behavior 2.4.
- It preserves the feature (3.9) rather than removing it: a model intended for
  two-image anomaly reference generation is authored with
  `limit_mm_per_prompt.image = 2`, the Fit_Check sizes it with the larger
  activation allowance, and the two-image path works. What disappears is
  *silently* enlarging an already-published model's profiling peak.
- It fixes the regression with zero re-publishing. The broken device's staged
  `model.json` omits the key; with the setdefault gone the demand is 1.0.59's
  again.
- Truthful failure over silent degradation for the two-image request: the
  anomaly-reference contract is "compare the input against this reference". A
  one-image degradation returns a confident answer to a *different* question —
  a wrong-but-plausible anomaly verdict, which is worse in a defect-detection
  product than a loud failure with an exact remediation. The failure is also
  cheap to fix (one engine-config field plus a re-publish) and cannot happen
  silently, because the model's own configuration states the limit.
- Naming the field exactly `limit_mm_per_prompt` (rather than a portal-specific
  alias like `limit_mm_per_prompt_image`) keeps preservation 3.3's *verbatim*
  propagation into `model.json` literally true and needs no change in
  `packaging.py`; `_decimal_to_native` / `_to_dynamo_compatible` already recurse
  into nested maps.

**Rejected alternatives.**

- *Keep the device-side default at 2.* Rejected: it is the leading regression
  cause, it is invisible to sizing, and it charges every vision-language model on
  every device for a capability most workflows do not use.
- *Make the device default budget-aware (apply 2 only when the measured budget
  allows).* Considered seriously — it self-tunes and needs no re-publish. Rejected
  as the primary mechanism because it makes a model's *capabilities* a function
  of transient device state: the same published model would accept two-image
  requests on a quiet device and reject them minutes later (exactly the
  non-determinism of defect 1.7, moved into the feature contract). It also
  requires an activation estimate to be trusted as a *permissive* gate, whereas
  every estimate in this design is used conservatively (to refuse). The authored
  field gives one answer per published model.
- *Degrade a two-image request to one image with a warning.* Rejected: silently
  answers a different question (see above).
- *Author it as a boolean capability flag (`anomaly_reference_enabled`).*
  Rejected: `limit_mm_per_prompt` is the actual `EngineArgs` field; a bespoke flag
  needs translation in `packaging.py`, breaks the verbatim-propagation property,
  and hides the real knob from operators reading `model.json` on a device.

**Honesty note.** The 4.92 GiB activation peak is the **one-image, video-unbound**
number measured on 1.0.59 at `util = 0.4`. The two-image peak on 1.0.61 was never
measured (the image was pruned from the device) and **still has not been** — no
`{"image": 2, …}` configuration has been profiled on this hardware, so
`MULTIMODAL_IMAGE_INCREMENT` remains an **[ESTIMATE, HARDWARE H8 to calibrate]**
in the *per-additional-image* direction.

**Amended 2026-08-19 — what the new measurements do and do not settle.** The two
1.0.62 runs above give the *unit* term one real data point pair at
`util = 0.55`: one unit (`{'image': 1, 'video': 0}`) → 2.47 GiB, two units
(`{'image': 1}`, video at vLLM's default of 1) → 4.93 GiB. The **ratio** the
formula encodes is therefore confirmed for units (2 units cost ≈2× one unit,
within 0.01 GiB), while `ACTIVATION_WEIGHT_FRACTION = 0.75` is shown to be
roughly **2× too high per unit** (the measurements imply ≈0.375 of weights:
2.47 GiB against 6.59 GiB). That recalibration is **deferred, not forgotten** —
the constant is mirrored in `src/backend/vllm_runtime/memory_budget.py` and pinned
equal by the Property 8 parity test, and the device mirror ships **only** via an
`aws.edgeml.dda.LocalServer.arm64JP6` component build, so both legs must move
together (spec task 14 / H8). Until then the portal allowance stays deliberately
high in the refusing direction, which is the intended posture. **The design's
correctness does not depend on the coefficient's precision** — the device
preflight and the on-hardware verification tasks are what prove the outcome.

**Stated limitation of the portal model (not papered over).** Because the
allowance is `max(2 GiB, 0.75 × weights) × units`, the portal predicts **4.94 GiB
for the one-unit case and 9.89 GiB for the two-unit case** against the measured
2.47 and 4.93 GiB. It reproduces the measured **direction and 2:1 ratio exactly**
and therefore refuses the unbounded-video configuration more readily than the
bounded one; it **cannot express the measured absolute 2.47 vs 4.93 GiB pair** at
the current coefficient. No coefficient is invented here to make it fit — the
single calibration point lands in H8 with the device mirror.

### Decision 2: a sound Fit_Check — activation allowance, co-tenancy cap, honest profile semantics, and a per-arch publish gate

**Decision.** Replace the one-line verdict with two named conditions over
documented terms.

```
// units := limit_mm_per_prompt.image (default 1) + limit_mm_per_prompt.video
//          (default 1 — vLLM's own per-modality default when the key is ABSENT;
//           an authored "video": 0 contributes 0). Amended 2026-08-19.
activation_allowance(weights, units) :=
    max(ACTIVATION_FLOOR_BYTES, ACTIVATION_WEIGHT_FRACTION * weights)
    * (1 + MULTIMODAL_IMAGE_INCREMENT * (units - 1))

required := weights + activation_allowance(weights, units) + MINIMUM_KV_CACHE
budget   := util * DEVICE_MEMORY_PROFILE_BYTES[arch]
cap      := (DEVICE_MEMORY_PROFILE_BYTES[arch]
             - CO_TENANCY_RESERVATION_BYTES[arch]) / DEVICE_MEMORY_PROFILE_BYTES[arch]

A (budget sufficiency) : budget  >= required
B (co-tenancy safety)  : util    <= cap        // == budget + co_tenancy <= profile
fits := A AND B
```

Constants, with their provenance:

| Constant | `arm64_jp6` | `arm64_jp7` | Provenance |
|---|---|---|---|
| `DEVICE_MEMORY_PROFILE_BYTES` | 30 GiB | 120 GiB | **Value unchanged** (satisfies sibling Requirement 3.8 literally); **re-documented** as TOTAL device memory as the engine sees it — reconciled against `free -g` total 29 GB and vLLM's own four terms summing to ≈29.95 GiB. |
| `MINIMUM_KV_CACHE_BYTES` | 1 GiB | 1 GiB | Value unchanged; re-documented as a *serving-margin floor*, not a hard load threshold — 0.65 GiB demonstrably served at 2.95x for 4096 tokens. Breaching it is the Thin_Margin warning (Decision 6). |
| `ACTIVATION_FLOOR_BYTES` | 2 GiB | 2 GiB | Conservative floor for small models where a fraction-of-weights term would round to nothing. Estimate. |
| `ACTIVATION_WEIGHT_FRACTION` | 0.75 | 0.75 | Calibrated to the then-single measured point: 4.92 GiB peak against 6.47 GiB of weights = 0.76, `enforce_eager=true`, `max_model_len=4096` — which is now understood to have been a **two-unit** (video-unbound) measurement. The 2026-08-19 pair implies ≈**0.375 per unit**, i.e. this constant is ~2× high. **Recalibration DEFERRED to task 14 / H8** because the value is mirrored in the device module and pinned by the Property 8 parity test; both legs move together, and the device leg needs a JP6 component build. Conservative in the refusing direction meanwhile. |
| `MULTIMODAL_IMAGE_INCREMENT` | 1.0 / extra **unit** | 1.0 | Per-**unit** ratio **confirmed** by the 2026-08-19 pair (2 units ≈ 2× one unit, within 0.01 GiB). Per-additional-**image** still **[ESTIMATE, H8]** — no `image ≥ 2` configuration has been profiled. Deliberately high so multi-unit configurations must be sized explicitly. |
| `CO_TENANCY_RESERVATION_BYTES` | 6 GiB | 8 GiB | JP6 measured: 5.7 GiB in the three ONNX Triton stubs (`ps -eo rss`: 3,909,200 + 1,030,612 + 921,184 KB) plus containers; `free -g` showed 6 GB used at a clean backend restart with no engine. JP7 is an **estimate** (thor1 co-residency not measured) chosen where JP6-style headroom analysis cannot flip a JP7 verdict at the utilizations in use. |

How the activation allowance is derived — and why it is a fraction of weights
rather than something cleverer: a per-model-class table would need per-class
measurements we do not have; a fixed absolute allowance would be wrong by an
order of magnitude across the 0.5B–70B range; a first-principles computation
(hidden size × batch tokens × layers) needs config fields the Fit_Check does not
fetch and would produce false precision. A fraction of weights tracks model scale
with one calibrated coefficient, is trivially auditable in the message, and errs
high. It is an **estimate and is labelled as one in every message**. The design
does not rely on it being accurate — it relies on it being conservative, with
Decision 4's device preflight as the truth check.

Why co-tenancy is a **cap on the fraction (B)** and not an addend to `required`:
vLLM charges resident foreign memory against its budget through the variable
`non_torch_memory` term, which swung from −0.05 GiB to 8.29 GiB on the same
device four minutes apart. Adding a fixed 6 GiB to `required` would encode that
worst case as a certainty and would refuse the configuration that demonstrably
serves today (12.00 GiB budget vs 18.38 GiB "required") — false precision in the
refusing direction, and it would make the success condition unreachable.
Modelling it as a cap answers the question that *is* deterministic: "does this
fraction, by construction, claim memory the co-tenants hold?" That is exactly
the hazard in defects 1.2/1.3 and it is what protects the ONNX models.

**Worked verdicts** (sanity checks, all from repo constants + measured numbers):

- Incident, `util=0.4`, `{'image': 1, 'video': 0}` = **1 unit**:
  `budget = 12.00`, `activation = max(2, 0.75×6.5) = 4.88`,
  `required = 6.5 + 4.88 + 1 = 12.38` → **A fails by 0.38 GiB**, B passes
  (0.4 ≤ 0.80). Verdict: does not fit — and the near-miss magnitude matches the
  device's 0.65 GiB remainder against the 1 GiB floor. The corrected model
  reproduces reality where the old one claimed 4.50 GiB of slack.
- Same model, `{'image': 1}` with **video unbound** = **2 units** (amended
  2026-08-19): `activation = 9.75`, `required = 17.25` → A fails by 5.25 GiB.
  This is the configuration the device measured at a 4.93 GiB peak with 0.20 GiB
  of KV, so the refusal is the right direction — the *magnitude* is ~2× the
  measured peak (see Decision 1's stated limitation).
- Same model, `{'image': 2, 'video': 0}` = **2 units**: identical arithmetic
  (`activation = 9.75`, `required = 17.25`). The 1.0.61 regression is visible at
  authoring time. **[ESTIMATE — no `image = 2` profile exists; H8.]**
- JP7 `qwen3-vl-8b-instruct`, `util=0.5`, 1 unit, ~16 GiB weights: `budget =
  60.00`, `required = 16 + 12 + 1 = 29.00` → fits; `0.5 ≤ 0.933`. **JP7 verdict
  unchanged** — and it stays unchanged even at 2 units
  (`required = 16 + 24 + 1 = 41.00 ≤ 60.00`), so the units amendment cannot flip
  a JP7 record that fits today (preservation 3.4).
- The sibling spec's original incident (Qwen2.5-7B bf16, 14.25 GiB, `util=0.3`):
  fails under both the old and the new model. Its remediation stays correct.

**Per-arch publish gate.** `greengrass_publish.py` blocks when **any** supported
architecture fails, not only when all do:

```
failing := [f for f in findings if not f.fits]
if failing and not skip_fit_check:  -> 422 with the per-arch findings
if failing and skip_fit_check:      -> proceed, status 'overridden', audited
else:                               -> status 'passed', or 'warnings' when a
                                       finding carries soft warnings (thin
                                       margin / near the Fraction_Cap)
```

The `skip_fit_check` override, its audit-event recording, and the `unverified`
path are preserved exactly. `warnings` keeps a meaning (fits, but with a recorded
caution) rather than becoming dead.

**Sibling-spec items that must be consciously repointed — recorded verbatim
before the change.** This design revises `vllm-sizing-and-packaging-errors`; the
following must be updated in the same change, and its Requirement text amended to
point at this spec:

| # | Location | Recorded verbatim | Disposition |
|---|---|---|---|
| S1 | `vllm-sizing-and-packaging-errors/requirements.md` R3.1 / R3.6 | "`fits = gpu_memory_utilization * DEVICE_MEMORY_PROFILE_BYTES[arch] >= weight_estimate + MINIMUM_KV_CACHE_BYTES`" (the decision and message contract) | **Revised** by conditions A+B; amend the requirement to cite this spec. |
| S2 | same, R3.8 | "THE Portal SHALL maintain the Device_Memory_Profile as a per-Target_Architecture table in code with at least an `arm64_jp6` entry of 30 GiB usable memory, and every Fit_Check message SHALL name the profile entry used." | **Value kept (30 GiB), semantics corrected**: the entry is TOTAL, not usable; messages keep naming the entry. Amend "usable" → "total device memory as the engine sees it". |
| S3 | same, R3.9 | "…the weights do not fit inside the configured fraction, so `gpu_memory_utilization` must be raised or the model shrunk — never advise lowering it for this failure mode" | **Narrowed** to the weights-exceed-budget arithmetic; superseded by Decision 3 for the activation/co-tenancy failure mode. |
| S4 | same, R4.2 | device-side hint "…stating that the value must be raised or the model reduced" | **Revised** by Decision 3's ordered remediation menu. |
| S5 | `edge-cv-portal/backend/tests/test_property_fit_check_decision.py:101` | `assert finding.required_bytes == required_bytes` where `required_bytes = estimate_bytes + MINIMUM_KV_CACHE_BYTES`; `expected_fits = budget_bytes >= required_bytes`; `assert re.search(r"raise\s+gpu_memory_utilization", finding.message, re.IGNORECASE)`; `assert not re.search(r"(lower|decrease|reduce)\w*\s+gpu_memory_utilization", …)` | **Repointed**: required includes the activation allowance; `fits` is A∧B; the message assertion becomes "names the activation and co-tenancy terms, leads with demand-reducing remediation, and mentions raising the fraction only with the cap stated". Note the old negative assertion forbids the string "reduce … gpu_memory_utilization" — the new message must still never advise *lowering* the fraction as a fix for insufficient KV, so that assertion is **kept**. |
| S6 | `edge-cv-portal/backend/tests/test_vllm_publish_fit_gate.py:286` `test_all_arch_failure_blocks_publish_with_422` | all-arch failure → 422 | **Kept, plus** a new any-arch case; the all-arch case must keep passing. |
| S7 | `edge-cv-portal/backend/tests/test_property_engine_config_update_roundtrip.py:36-64` and `test_property_engine_config_invalid_updates.py:59` | `KNOWN_ENGINE_KEYS = ("dtype", "gpu_memory_utilization", "max_model_len", "tensor_parallel_size", "enforce_eager")` … `assert set(model_import.ENGINE_DEFAULTS) == set(KNOWN_ENGINE_KEYS)` | **Repointed**: add `limit_mm_per_prompt`. The guard's purpose (catch drift) is preserved. |
| S8 | `test_vllm_engine_config_detail_and_audit.py` (`assert_config_equals`) and any test asserting a literal resolved configuration | `assert set(actual) == set(expected)` over the resolved configuration | **Repointed**: expected literals gain `limit_mm_per_prompt: {"image": 1, "video": 0}` (**amended 2026-08-19**; the value first landed as `{"image": 1}` and was widened by the video bound — Decision 1 "Schema revision"). |
| S9 | `test_property_fit_unverified_never_blocks.py`, `test_vllm_fit_check_estimation.py` | unverified/estimation behavior | **Unchanged** — must keep passing untouched (preservation 3.2). |

**Rejected alternatives (fit model).**

- *Just raise `MINIMUM_KV_CACHE_BYTES`.* Rejected: the floor is not the error, and
  the device proved a sub-floor load can serve. It would also mis-scale (a fixed
  floor cannot stand in for a term proportional to model size).
- *Lower `DEVICE_MEMORY_PROFILE_BYTES[arm64_jp6]` to ~24 GiB to bake in
  co-tenancy.* Rejected: it silently corrupts the meaning of
  `gpu_memory_utilization` (a fraction the *device* applies to its real total),
  so the portal's budget would no longer be the number vLLM targets, and every
  message would state a memory figure the device does not have. Sibling
  Requirement 3.8's 30 GiB entry is also correct as a TOTAL.
- *Fetch each model's config and compute activations from first principles.*
  Rejected: needs fields the estimator does not fetch, adds latency to a
  registration path bound by a 5 s timeout, and produces false precision for a
  term that swings with runtime state anyway.
- *Keep the all-arch publish gate and rely on warnings.* Rejected: it is defect
  1.8 — an `arm64_jp6`-infeasible configuration ships because JP7 fits, which is
  exactly how this incident reached the fleet.

**Stated consequence.** The corrected model is strictly more conservative, so
some configurations that publish today will be refused (e.g. a JP7 record with
34–59 GiB of weights at `util=0.5`, or any JP6 record above the 0.80 cap). That
is the intended fail-closed direction; the audited `skip_fit_check` override
remains the escape hatch, and no known record is in those bands.

### Decision 3: remediation guidance that is not a hazard

**Decision.** Both surfaces (`vllm_fit_check.evaluate_fit` messages and
`vllm_model_prep.log_load_failure`) emit the same ordered menu, from one shared
wording contract:

1. **Hazard first** — one sentence: this device shares unified memory with the
   ONNX GPU models; `gpu_memory_utilization` is a fraction of TOTAL memory, so
   raising it takes memory those models are using.
2. **Demand-reducing remediations, in order**: bound the multimodal limit
   (`limit_mm_per_prompt.image`, biggest single lever for a VLM); reduce
   `max_model_len`; choose a smaller or more quantized model; free device memory
   (stop unused model components).
3. **Raise the fraction — last, conditional, quantified**: offered only when
   `util < cap`, and always as a concrete bounded range ("`gpu_memory_utilization`
   may be raised to at most 0.80 on `arm64_jp6` — 30 GiB total minus 6 GiB held by
   co-resident models; the budget you need is 12.38 GiB, i.e. at least 0.42").
   When `util >= cap`, the surfaces say raising it is unsafe here and stop.

The message always names the terms and their numbers (weights, activation
allowance *labelled as an estimate*, KV floor, budget, co-tenancy reservation,
cap) so an operator can audit the verdict instead of trusting it. The device-side
line keeps carrying model name, HTTP status, extracted reason and the staged
`gpu_memory_utilization` / `max_model_len` (3.8), and still never advises
*lowering* the fraction as a cure for insufficient KV.

**Rationale.** Defect 1.3 is a guidance hazard, not a wording nit: following
today's advice on this device converts one broken model into a broken vision
stack, and success condition 2.10 makes that a failure. Ordering by "does this
reduce our own demand or take memory from someone else" is the only ordering that
cannot violate the conjunction.

**Rejected alternatives.** *Never mention raising the fraction.* Rejected: for
the sibling spec's weights-exceed-budget arithmetic it is the correct fix, and
suppressing it would strand those operators. *Auto-tune the fraction at publish
time.* Rejected: it writes a memory policy for a fleet of devices from one
device's numbers, and the staged args are consumed verbatim by design (3.3).

### Decision 4: device-side preflight — one authority in the manager, classified by the prep

**Decision.**

- **A new pure module** `src/backend/vllm_runtime/memory_budget.py` holds the
  device-side sizing math and the memory reader. It is stdlib-only, imports no
  torch, and **never touches CUDA** — availability comes from `/proc/meminfo`
  (`MemTotal`, `MemAvailable`). This is non-negotiable: a CUDA-initializing probe
  in the parent backend process poisons every subsequently forked child
  (`vllm-jp7-engine-cuda-init` defect 1.3), and the manager's existing reclaim
  invariant already encodes that rule.
- **The authoritative check runs in the manager**, in `load()`, after
  `parse_repository` and **before** engine construction. It refuses with
  `FAILED(reason)` where the reason begins with the stable marker
  `preflight-refused:` and names measured-available, computed-requirement with
  every term, and the specific setting to change. Cost: one file read plus one
  directory stat walk — seconds, not the ~4 min of profiling that blocks the
  runtime server's event loop.
- **The prep does not run its own memory check.** It classifies the returned
  reason: a new `LOAD_PREFLIGHT_REFUSED` outcome, checked **before** the
  `KV_CACHE_HINT_MARKERS` test (the preflight diagnostic legitimately contains the
  string `gpu_memory_utilization`, which would otherwise trigger the KV-OOM
  unload→reload recovery for a load that never allocated anything).
- **Refusal conditions** (any one refuses):
  - `P1 starvation`: `available < weights + activation_allowance + KV_floor`.
  - `P2 budget`: `util × MemTotal < weights + activation_allowance + KV_floor`
    (condition A re-evaluated against the device's real total).
  - `P3 latch`: the Starvation_Latch from Decision 5 is set for this backend life.
- **Weights on device**: sized from disk — the resolved `model` path when it is a
  local directory (S3-sourced records), else the Hugging Face cache snapshot
  (`models--{org}--{name}`) under the configured cache root. When neither is
  determinable, the weights-dependent arms degrade to the
  `ACTIVATION_FLOOR + KV_floor` lower bound and the log says the check ran
  **unverified** — the same honesty rule the portal uses, never a guessed number.
- **Deployment consequence — chosen:** a preflight refusal exits **0** with a
  prominent ERROR line, so the model is reported FAILED-with-reason through the
  existing status contract while the deployment succeeds. Every other outcome
  keeps today's exit codes exactly (`LOAD_UNREACHABLE` → 1, `LOAD_HTTP_ERROR` → 1,
  `LOAD_OK` → 0), preserving 3.8.

**Rationale for exit 0 on preflight refusal.** The verdict is deterministic,
self-describing, and produced before any allocation: retrying cannot change it,
so the retry-and-fail-the-deployment machinery buys nothing and costs
everything — defect 1.9, where one mis-sized model takes revision 73 to
`FAILED_ROLLBACK_COMPLETE`, blocks every unrelated change for that device, and
leaves the latest cloud deployment a FAILED revision that future revisions
preload from. Nothing is shipped silently: there is a prominent ERROR in the
component log, a `FAILED` model state carrying the full reason through the
unchanged status surfaces, and the portal's fit check refusing the same
configuration at publish time. Truthful-FAILED-with-successful-deployment beats
a rollback that hides the reason behind a component-retry storm. Non-deterministic
failures (unreachable runtime, real HTTP errors) keep exit 1 because a retry can
genuinely fix them.

**Rejected alternatives.**

- *Check in the prep before the load POST (or in both places).* Rejected: two
  verdicts drift, and the prep cannot see what the manager already holds (other
  loaded engines, the Starvation_Latch, the parsed args after repository
  validation). One authority, one diagnostic, one place to test.
- *Check in `repository.py` at parse time.* Rejected: that file is explicitly not
  changed, and it is the wrong layer — parsing is about the repository contract,
  not device state.
- *Refuse by returning a distinct HTTP status.* Rejected: the runtime server's
  routes and status maps are explicitly not changed; the existing 409-with-reason
  carries everything needed.
- *Exit 1 always (today's behavior).* Rejected above; it is the mechanism of
  defect 1.9.
- *Exit 0 for every load failure.* Rejected: it would resurrect the
  "healthy but never loaded" hole that `edge-deploy-reliability` Defect D closed
  for genuinely transient failures.

### Decision 5: stranded-allocation cascade — reclaim what we can, refuse what we cannot

**Decision.** Keep `_reclaim_gpu_memory` as-is on the paths where it works
(including its CUDA-init invariant), and add a *measured* verdict around every
failed attempt:

1. Before engine construction the manager records `available_before` from
   `/proc/meminfo`.
2. On any load failure, after `_shutdown_engine` + `_reclaim_gpu_memory`, it
   records `available_after`.
3. If `available_after < available_before − RECLAIM_TOLERANCE_BYTES` (proposed
   0.5 GiB), it sets the **Starvation_Latch** for this backend life, with the two
   readings and the model name, and logs a prominent WARNING stating that the
   failed attempt's memory did not come back and that a backend container restart
   is required to recover.
4. While the latch is set, the preflight refuses further loads (P3) with a
   diagnostic naming the condition and the readings — instead of retrying into a
   starved device. The prep's single KV-OOM unload→reload recovery is preserved
   (3.8) but its second attempt is refused in seconds when the device is
   demonstrably starved, which is the cascade's stopping condition.
5. An explicit `unload` clears the latch (an operator-initiated cycle is allowed
   to try again), and it is per-backend-life state only — no new persisted
   contract, no tombstone interaction.

**Rationale.** The evidence supports one thing without ambiguity: reclaim works
on the KV-OOM path (`Reclaimed cached CUDA memory` cleared an 8.34 GiB non-torch
swing) and does not on the NVML-assert path (three failures → 26 GB used / 3 GB
free with no model loaded, recovered only by a container restart). Making reclaim
cover the assert path would require knowing why the assert happens — an open
question (Decision 6). Measuring whether memory came back needs no such
knowledge, is honest, and directly satisfies expected behavior 2.5's second
branch ("or SHALL detect that it cannot and refuse to retry into a starved
device with a diagnostic"). The decision logic is fully host-testable over
injected readings; the actual reclaim is **[HARDWARE]**.

**Rejected alternatives.** *Call `torch.cuda.empty_cache()` unconditionally
(drop the `is_initialized()` gate).* Rejected: it would initialize CUDA in the
parent and poison forked children — a known, separately-diagnosed defect.
*Auto-restart the backend container on starvation.* Rejected: the runtime does
not own its container lifecycle, and a self-restarting backend would take every
other model with it. *Kill and re-exec the runtime process.* Rejected: same blast
radius, and the reconciler's validated post-restart behavior is another spec's
contract.

### Decision 6: thin-margin and symptom distinguishability — inside existing surfaces only

**Decision — surfaces.**

- **Thin margin (2.7)**: after a load reaches READY, the manager best-effort reads
  the engine's KV sizing (`num_gpu_blocks`, `block_size`, `max_model_len` via a
  small `getattr` chain over `engine.engine.cache_config`, wrapped so any exotic
  engine shape simply yields "unknown"). When the derived KV bytes are below
  `MINIMUM_KV_CACHE_BYTES`, or the derived maximum concurrency is below
  `THIN_MARGIN_CONCURRENCY` (proposed 2.0x), it logs a prominent **WARNING**
  naming the margin, the concurrency, and the fact that the load is one retry
  from failing. READY is still READY.
- **Symptom distinguishability (2.6)**: a pure classifier maps a failure reason
  to a stable **category token prepended to the existing `reason` string**, with
  the original reason preserved verbatim after it:
  `kv-cache-exhaustion: …`, `allocator-nvml-fault: …`, `preflight-refused: …`,
  `repository-invalid: …`, `engine-construction-error: …`. The `reason` field, the
  409 body and every status map stay structurally identical, and the prep's
  existing `KV_CACHE_HINT_MARKERS` matching keeps working because the original
  text is intact (with the precedence rule from Decision 4).

**No new status surfaces.** No field is added to `ModelStatus`, no route or status
mapping in `repository.py` / the runtime server changes (both explicitly not
changed), and nothing is added to `/feature-configurations` or the model-status
shadow. **Overlap avoidance with `model-gpu-fallback-visibility`:** that spec owns
per-model GPU-fallback flags and the device-level degraded-GPU signal in the
model-status payloads, and its own scope statement excludes vLLM paths. This
spec's signals are vLLM-only, live in the LocalServer component log and in the
existing `FAILED(reason)` text, and are therefore disjoint from that spec's
payload work. If a future need arises to publish vLLM margin data in the
model-status shadow, it belongs in that spec, not this one.

**The NVML assert stays an open question.** `NVML_SUCCESS == r INTERNAL ASSERT
FAILED at "/opt/pytorch/c10/cuda/CUDACachingAllocator.cpp":1131` occurred on
13:36:30Z, 13:39:38Z and 21:44Z — in all three cases *after* a previous failed
attempt in the same backend life — while the single clean-system attempt produced
the KV-cache message. That correlation is suggestive (an exhaustion symptom) but
3/3 is not a determination, and it is a different symptom from the
`cudaErrorDevicesUnavailable` class owned by `vllm-jp7-engine-cuda-init`.
**Recorded determination path** (a later USER ACTION task, **[HARDWARE]**):

1. On a device with a fixed component, capture the full Python traceback and the
   surrounding vLLM log for an assert occurrence (currently only the assert line
   is recorded).
2. Record `/proc/meminfo` `MemAvailable` immediately before each attempt and after
   each failure (the new preflight/latch logging does this by construction).
3. Determine whether the assert ever occurs as the **first** attempt of a clean
   backend life at a utilization that A+B accept. If never → exhaustion symptom;
   if it does → distinct fault, and the finding is handed to a new spec (not to
   `vllm-jp7-engine-cuda-init`, whose class is different).
4. Cross-check the JP7 spec's evidence for any shared signature before attributing
   a cause. Do not attribute one before step 3 completes.

**Rejected alternatives.** *Add a `warnings` list to `ModelStatus` and surface it
in the 409/status payloads.* Rejected: it changes the load/unload contract this
spec is only a consumer of, and it would collide with another spec's payload
work. *Log the thin margin at INFO.* Rejected: defect 1.6 is that a
one-retry-from-failure device looks healthy; INFO is where that already hides.
*Parse vLLM's own log lines to recover the KV remainder.* Rejected: log-scraping
a third-party format is brittle; the cache config is a structured read behind a
best-effort guard.

## Correctness Properties

Property 1: Bug Condition - Unsound Fit_Check verdict

_For any_ vLLM record and architecture where the bug condition holds (the shipped
formula reports `fits` while the corrected required-bytes exceed the budget, or
the configured fraction exceeds the architecture's Fraction_Cap), the fixed
`evaluate_fit` SHALL return `fits = False` for that architecture, SHALL state the
weights, activation allowance (labelled an estimate), KV floor, budget,
co-tenancy reservation and cap with their numbers in the message, and SHALL NOT
present raising `gpu_memory_utilization` as a remediation unless the resulting
fraction stays at or below the cap.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation - Non-buggy inputs are byte-identical

_For any_ input where the bug condition does NOT hold (a record that fits under
both the old and new models, an undeterminable Weight_Estimate, a non-vLLM
publish, a JP7 record within its headroom, a load with a healthy margin), the
fixed pipeline SHALL produce the same result as the original: the same publish
status vocabulary and `fit_check` annotation, the same audit events, the same
`unverified` non-blocking behavior, a byte-identical staged `model.json` for the
five pre-existing settings, the same prep exit-code classification, and unchanged
reconciler behavior.

**Validates: Requirements 3.1, 3.2, 3.3, 3.7, 3.8, 3.10**

Property 3: Bug Condition - Per-architecture publish gate

_For any_ findings set where at least one supported architecture fails, the fixed
publish SHALL refuse with 422 and the per-architecture findings unless
`skip_fit_check` is supplied, in which case it SHALL proceed with status
`overridden` and record the override in the audit event.

**Validates: Requirements 2.8, 3.1**

Property 4: Bug Condition - The multimodal limit is authored and budgeted

_For any_ staged engine args, the fixed runtime SHALL NOT inject a
`limit_mm_per_prompt` value that the authored configuration did not specify; the
effective limit SHALL be visible in the staged `model.json` and SHALL be the
multimodal term the Fit_Check sizes; and a two-image request against a model
authored for one image SHALL fail with a diagnostic naming the limit and the
remediation rather than silently using one image.

**Validates: Requirements 2.4, 3.9**

Property 5: Bug Condition - Device preflight fails fast and truthfully

_For any_ injected memory reading and staged args where the requirement exceeds
either the measured available memory or the device-computed budget, the fixed
manager SHALL refuse before engine construction with a reason carrying the
`preflight-refused:` marker, the measured available bytes, the computed
requirement with its terms, and the specific setting to change; and the prep
SHALL classify that outcome as `LOAD_PREFLIGHT_REFUSED`, skip the KV-OOM
unload→reload recovery, and exit 0 while every other classification keeps its
current exit code.

**Validates: Requirements 2.9, 3.8**

Property 6: Bug Condition - No retry into a starved device

_For any_ sequence of injected before/after memory readings around a failed load
where the available memory does not return to within the reclaim tolerance, the
fixed manager SHALL set the Starvation_Latch, log the two readings, and refuse
subsequent load attempts in that backend life with a diagnostic naming the
starved condition, until an explicit unload clears it.

**Validates: Requirements 2.5**

Property 7: Bug Condition - Distinguishable symptoms and visible thin margins

_For any_ failure reason, the fixed manager SHALL prepend exactly one stable
category token (`kv-cache-exhaustion`, `allocator-nvml-fault`,
`preflight-refused`, `repository-invalid`, `engine-construction-error`) while
preserving the original reason text verbatim so existing marker matching still
works; and _for any_ engine whose post-load KV sizing is readable and below the
floor or the thin-margin concurrency, it SHALL log a WARNING naming the margin
rather than reporting an unqualified success.

**Validates: Requirements 2.6, 2.7**

Property 8: Preservation - Portal and device sizing models agree

_For any_ point on a grid of (architecture, weights, utilization, images), the
portal's `vllm_fit_check` budget model and the device's `memory_budget` model
SHALL compute the same required bytes and the same budget-sufficiency verdict, so
a configuration accepted at publish time is never refused by the device for a
reason the portal could have predicted.

**Amended 2026-08-19 — the one axis where they deliberately differ.** The parity
grid is over the **image** term, and parity there is exact. The portal now also
prices an **unauthored `limit_mm_per_prompt.video`** as a second multimodal unit,
which the device mirror does not yet do: the mirror lives in
`src/backend/vllm_runtime/memory_budget.py` and ships only via an
`aws.edgeml.dda.LocalServer.arm64JP6` component build, which has **not** run for
this change. The divergence is one-directional and safe: the portal is the **more
conservative** of the two on that axis, so it can only refuse earlier than the
device, never accept something the device then refuses for a predictable reason.
Both legs converge when the device build lands (with the H8 recalibration, task
14).

**Validates: Requirements 2.1, 2.9**

Property 9: Preservation - JP7 and the JP6 ONNX models are untouched

_For any_ JP7 vLLM load and _for any_ of the three co-resident JP6 ONNX GPU
models, the fixed component SHALL produce the same behavior as the original —
`qwen3-vl-8b-instruct` loading with 36.34 GiB of KV cache under
`gpu_memory_utilization=0.5`, and the three ONNX models reaching READY on GPU
with unchanged inference behavior and footprint. **[HARDWARE]**

**Validates: Requirements 3.4, 3.6, 3.11**

## Fix Implementation

### Defect → File map (every clause 1.1–1.10 accounted for)

| Defect | Disposition | Where |
|---|---|---|
| 1.1 Fit_Check omits activation peak and other consumers | Fixed | **File 1** (formula + message), **File 5** (device mirror) |
| 1.2 Budget is a fraction of TOTAL with no co-tenancy notion | Fixed | **File 1** (Fraction_Cap condition B, profile re-documented) |
| 1.3 Both surfaces advise raising the fraction | Fixed | **File 1** (portal message), **File 6** (device message) |
| 1.4 Unbudgeted device-side `limit_mm_per_prompt = 2` | Fixed | **File 4** (default removed, two-image request validated), **File 3** (authored field), **File 1** (sized) |
| 1.5 Stranded allocations across the NVML-assert path | Fixed (detect-and-refuse; reclaim itself unchanged) | **File 4** (Starvation_Latch), **File 5** (readings) |
| 1.6 NVML assert indistinguishable from KV exhaustion | Fixed for reporting; **root cause deferred** to a recorded determination task (Decision 6) | **File 4** (classifier) |
| 1.7 Same args, different outcome; thin margin unreported | Fixed for reporting (the variance itself is a device property, not removable) | **File 4** (thin-margin WARNING), **File 1** (verdict reflects the near-miss) |
| 1.8 Per-arch publish escape (`every_arch_fails`) | Fixed | **File 2** |
| 1.9 One mis-sized model fails the whole deployment | Fixed for the deterministic preflight case only (exit 0 + FAILED-with-reason); other failure classes keep exit 1 by design | **File 6**, **File 4** |
| 1.10 Engine args never checked against actual free memory | Fixed | **File 5** (module), **File 4** (call site), **File 6** (classification) |

### File 1: `edge-cv-portal/backend/functions/vllm_fit_check.py`

*Portal leg. Not preservation-tracked. Defects 1.1, 1.2, 1.3, 1.4 (sizing half).*

1. Re-document `DEVICE_MEMORY_PROFILE_BYTES` as **total** device memory as the
   engine sees it (values unchanged; cite `free -g` total 29 GB and vLLM's four
   terms summing to ≈29.95 GiB for the `arm64_jp6` entry).
2. Add constants with provenance comments: `ACTIVATION_FLOOR_BYTES`,
   `ACTIVATION_WEIGHT_FRACTION`, `MULTIMODAL_IMAGE_INCREMENT`,
   `CO_TENANCY_RESERVATION_BYTES`, `DEFAULT_IMAGES_PER_PROMPT = 1`. Re-document
   `MINIMUM_KV_CACHE_BYTES` as a serving-margin floor (0.65 GiB demonstrably
   served) rather than a hard load threshold.
3. Add pure helpers: `activation_allowance(weights_bytes, multimodal_units)`,
   `fraction_cap(arch)`, `images_per_prompt(engine_configuration)` (reads
   `limit_mm_per_prompt.image`, tolerating `Decimal`/missing/malformed values by
   falling back to 1 — this module must never raise out of its public API) and —
   **amended 2026-08-19** — `videos_per_prompt(engine_configuration)` (reads
   `limit_mm_per_prompt.video`, falling back to `DEFAULT_VIDEOS_PER_PROMPT = 1`,
   vLLM's own per-modality default, so an unauthored bound is the expensive case),
   `video_is_authored(engine_configuration)` (whether the message must explain the
   omission) and `multimodal_units(engine_configuration)` = images + videos. The
   image term stays **separate and floored at 1**: an authored `"video": 0` must
   never be read as `images = 0`.
4. Rewrite `evaluate_fit`: `required = weights + activation_allowance + KV floor`;
   `fits = (budget >= required) AND (util <= fraction_cap(arch))`.
5. Extend `FitFinding` **additively**: keep `arch`, `fits`, `budget_bytes`,
   `required_bytes`, `message`; add `weights_bytes`, `activation_bytes`,
   `kv_floor_bytes`, `co_tenancy_bytes`, `fraction_cap`, `images_per_prompt`,
   `videos_per_prompt`, `multimodal_units`, `failed_conditions: List[str]`
   (`"budget"` / `"co_tenancy"`), and `warnings: List[str]` (e.g. `thin_margin`,
   `near_cap`). Existing consumers read only the original five fields; `asdict`
   keeps working.
6. Rewrite both message branches to Decision 3's ordered menu, always naming
   every term and labelling the activation allowance an estimate. Keep the
   invariant that no message ever advises *lowering* `gpu_memory_utilization` as a
   cure for insufficient KV.
7. Add a `warnings`-producing soft check: `fits` but the post-requirement margin
   is under the KV floor, or `util` is within 0.05 of the cap.

### File 2: `edge-cv-portal/backend/functions/greengrass_publish.py`

*Portal leg. Defect 1.8.*

1. Replace `every_arch_fails = all(not finding.fits …)` with
   `failing = [f for f in findings if not f.fits]`; block on `failing` (any
   architecture) with the same 422 body shape, now carrying only the failing
   architectures in the error text and all findings in `fit_check.findings`.
2. Preserve the `skip_fit_check` override branch verbatim in behavior (status
   `overridden`, warning log, `skip_fit_check: True` on the audit event) — only its
   trigger widens from all-arch to any-arch.
3. Keep the `unverified` branch and the `passed` / `warnings` split; `warnings`
   now means "every architecture fits, but at least one finding carries a soft
   warning".
4. Comment the gate with this spec's name so the next reader finds the revision,
   not the sibling spec's superseded rule.

### File 3: `edge-cv-portal/backend/functions/model_import.py`

*Portal leg. Defect 1.4 (authoring half), expected 2.4.*

1. `ENGINE_DEFAULTS['limit_mm_per_prompt'] = {'image': 1, 'video': 0}`
   (**amended 2026-08-19**; the single-key `{'image': 1}` form is superseded —
   Decision 1 "Schema revision").
2. `_validate_engine_setting`: accept a dict whose keys are a **non-empty subset
   of `{image, video}`** — `image` an int in 1..8, `video` an int in 0..8 (reject
   `bool` in both, reject unknown sub-keys, reject `{}`, reject non-ints) with a
   per-sub-key reason quoting that sub-key's range — the fail-closed rule for
   unknown engine keys is untouched.
3. `ENGINE_SETTINGS_SPEC` (the settings endpoint): add the field with type,
   default, both accepted ranges, `accepted_keys = ["image", "video"]`, and a
   description stating that raising a count increases the engine's profiling peak,
   that two-image reference generation (`vlm-anomaly-reference-parity`) requires
   `image: 2`, and that a model only ever asked for images should set
   `"video": 0` (measured on JP6: activation peak 4.93 GiB unbounded vs 2.47 GiB
   bounded). Both frontend forms are schema-driven off this endpoint, so the field
   renders with **no frontend wiring** — which also means an **unadvertised
   sub-key is unauthorable**, so `accepted_keys` must carry both.
4. `resolve_engine_configuration` needs no change (it overlays on
   `ENGINE_DEFAULTS`); confirm `_to_dynamo_compatible` recursion over the nested
   map (it already recurses).
5. `evaluate_fit_check` stays non-blocking and keeps its `passed` / `warnings` /
   `unverified` vocabulary.

### File 4: `src/backend/vllm_runtime/manager.py`

*Device leg. Not preservation-tracked. Defects 1.4, 1.5, 1.6, 1.7, 1.10.*

1. **Remove** `engine_args.setdefault("limit_mm_per_prompt", {"image": 2})`,
   replacing the comment with the reason (authored + sized in the engine
   configuration; see Decision 1) so the next reader does not re-add it.
2. In `load()`, after `parse_repository` and before `self._engine_factory(...)`:
   call `memory_budget.evaluate_device_fit(...)` with the parsed args, the
   Starvation_Latch state, and the injected readers. On refusal → `self._fail(
   model_name, "preflight-refused: …")` without constructing an engine.
3. Record `available_before` at that point; in `_fail`, after
   `_shutdown_engine` + `_reclaim_gpu_memory`, read `available_after`, and set the
   Starvation_Latch when the delta exceeds `RECLAIM_TOLERANCE_BYTES`, logging both
   readings. `unload()` clears the latch. Latch state is lock-guarded, per-backend
   life, not persisted.
4. Add the failure classifier and apply it in `_fail` so the reason gains exactly
   one category token prefix with the original text preserved verbatim.
5. After READY, best-effort KV-margin introspection
   (`engine.engine.cache_config` → `num_gpu_blocks`, `block_size`) behind a
   `getattr`/try guard; log the thin-margin WARNING when below the floor or below
   `THIN_MARGIN_CONCURRENCY`. Any unreadable shape → one debug line, no warning,
   no behavior change.
6. `_build_multimodal_prompt`: when `reference_bytes` is supplied and the tracked
   `engine_args`' effective `limit_mm_per_prompt.image` is < 2, raise
   `GenerationError` naming the model, the effective limit and the remediation —
   before the engine is invoked, in the same style as the existing decode-failure
   guards. The single-image path is untouched.
7. Injection seams: the memory reader and the KV-introspection accessor are
   module-level callables (or optional constructor arguments defaulting to the
   real ones), so host tests drive both with fakes and no GPU — matching the
   existing `engine_factory` / `sampling_params_factory` convention.

### File 5: `src/backend/vllm_runtime/memory_budget.py` (new)

*Device leg. Defects 1.10, 1.1 (device half), 1.5 (readings).*

1. Stdlib-only, **no torch, no CUDA, no vLLM import**. Module docstring states the
   invariant and why (`vllm-jp7-engine-cuda-init` defect 1.3).
2. `read_memory(reader=_default_proc_meminfo_reader) -> MemoryReading(total_bytes,
   available_bytes)` parsing `MemTotal` / `MemAvailable`; unparseable → `None`
   (callers degrade to "unverified", never raise).
3. Constants **mirrored** from File 1 with a comment naming it the single source
   of truth and pointing at the cross-check test (Property 8). Same
   `activation_allowance` formula, same KV floor.
4. `estimate_weights_on_disk(engine_args, hf_cache_roots=…)` → bytes or `None`:
   local directory (S3-sourced rewritten path) → sum of weight-file sizes
   (`*.safetensors`, `*.bin`, `*.gguf`); HF repo id → `models--{org}--{name}`
   snapshot under the cache roots; otherwise `None`.
5. `evaluate_device_fit(engine_args, reading, weights_bytes, latch) ->
   DeviceFitVerdict(ok, refusal_reason, terms, unverified)` implementing P1/P2/P3
   and composing the one-line diagnostic (measured available, computed requirement
   with terms, the specific setting to change, and — when `util` is at or above the
   cap — the co-tenancy hazard sentence).
6. The refusal string begins with `PREFLIGHT_REFUSED_MARKER = "preflight-refused:"`
   exported for the prep to match.

### File 6: `src/backend/dda_triton/vllm_model_prep.py`

*Device leg. Defects 1.3 (device half), 1.9, 1.10 (classification).*

1. Rewrite the `log_load_failure` KV remediation into Decision 3's ordered menu
   (hazard first, demand-reducing remediations, bounded fraction increase last).
   Keep the model name, HTTP status, extracted reason and the staged
   `gpu_memory_utilization` / `max_model_len` (3.8) and keep never advising a
   lower fraction.
2. Add `LOAD_PREFLIGHT_REFUSED` and match `PREFLIGHT_REFUSED_MARKER` **before**
   `KV_CACHE_HINT_MARKERS` in `request_load`, so a refusal never triggers the
   unload→reload recovery. `KV_CACHE_HINT_MARKERS` itself is unchanged.
3. In `prepare`: `LOAD_PREFLIGHT_REFUSED` → log the prominent ERROR (full
   diagnostic + "the deployment is not failed for this reason; the model is
   reported FAILED with its reason") and `return 0`. `LOAD_UNREACHABLE` and
   `LOAD_HTTP_ERROR` keep returning 1 with their existing text, `LOAD_OK` keeps 0.
4. The marker constant is duplicated (the prep cannot import `vllm_runtime` cleanly
   in every context) with a comment naming the owner and a host test asserting the
   two constants are equal.

### File 7: `edge-cv-portal/frontend/src/services/api.ts` (+ `ModelDetail.tsx` only if needed)

*Portal leg. Additive; supports 1.1/1.3 visibility.*

1. `VllmFitCheckFinding`: add the new fields as **optional** so existing rendering
   compiles unchanged.
2. If the fit-check panel is to show the term breakdown, render it from the new
   optional fields; otherwise the existing `message` already carries every number
   and no component change is required. Engine-setting forms need no change (they
   are generated from the settings endpoint).

### File 8: tests

1. New host tests for Properties 1–8 (see Testing Strategy).
2. Repointed sibling tests S5, S6, S7, S8 from Decision 2's table — repointed in
   the same change, with the verbatim originals recorded in this design and the
   sibling spec's requirement text amended to cite this spec. S9 must keep passing
   untouched.

**No preservation-tracked file is expected to change.** `manager.py`,
`vllm_model_prep.py`, the new `memory_budget.py`, and the portal functions are
not in the security-preservation baselines. If implementation reveals a genuine
need to touch `src/backend/Dockerfile*`, `src/backend/requirements.txt`,
`src/docker-compose.yaml`, a recipe variant, or
`station_install/setup_station.sh`, the baseline is rebaselined in the **same
change** per `.kiro/steering/builds.md` and the preservation suite is run in the
flask-app container **before** the build is started. The design deliberately
avoids new dependencies (`/proc/meminfo` parsing is stdlib) so this should not
arise.

## Testing Strategy

### Validation Approach

Two phases. First, surface counterexamples on the UNFIXED code — host-side for
the fit math, the publish gate, the engine-arg authoring/staging and the new
decision logic over injected readings; on hardware for everything that needs a
GPU. Then verify the fix and prove preservation. The honesty guard is binding: no
host test loads a real vLLM engine, allocates GPU memory, or simulates Jetson
unified-memory accounting.

**Host-testable seams (all of them explicit):**

| Seam | Mechanism |
|---|---|
| Fit math | Pure functions in `vllm_fit_check` — call directly (already the sibling suite's pattern). |
| Weight estimation | Injected `hf_fetch` / `s3_head` (existing seams; unchanged). |
| Publish gate branches | moto + the existing `FakeGreengrass` harness in `test_vllm_publish_fit_gate.py`; `estimate_weights` / `evaluate_fit` monkeypatched. |
| Engine-arg authoring & staging | `model_import` validation/resolution + `packaging.generate_vllm_repository` output inspected as JSON; `vllm_model_prep.stage_repository` against a tmp dir. |
| Device memory readings | `memory_budget.read_memory(reader=…)` — a fake reader returning crafted `/proc/meminfo` text; **the injection seam for every starvation/preflight test**. |
| Weights-on-disk probe | tmp directory trees with fake weight files / a fake HF cache layout. |
| Manager load path | existing `engine_factory` injection (a fake engine, or a factory that raises to simulate failure) + the injected memory reader + a fake `cache_config` object for KV introspection. |
| Prep classification & exit codes | `requests` monkeypatched to return crafted 409 bodies; `prepare()` return value asserted. |
| Portal/device parity | one host test importing both modules and comparing over a grid (Property 8). |

**[HARDWARE] tiers** (later USER ACTION tasks, in this order):

- **H1** — JP6 fixed component: the previously failing model loads to READY, and
  the vLLM log shows a one-image profiling peak comparable to 1.0.59's 4.92 GiB.
- **H2** — JP6 co-tenancy: the three ONNX models stay READY on GPU with unchanged
  inference behavior before, during and after the vLLM load (success condition
  2.10's second half).
- **H3** — preflight: a deliberately over-sized configuration is refused in
  seconds (not ~4 min), the runtime server stays responsive
  (`/v2/repository/index` answers throughout), and the Greengrass deployment
  **succeeds** with the model reported FAILED-with-reason.
- **H4** — starvation: after an induced failed load, the readings and the latch
  behave as designed and a retry is refused rather than starving the device.
- **H5** — thin margin: the WARNING appears for a load that reaches READY below
  the floor.
- **H6** — JP7 regression check: `qwen3-vl-8b-instruct` still loads with
  36.34 GiB of KV under `gpu_memory_utilization=0.5`.
- **H7** — NVML-assert determination (Decision 6's recorded path).
- **H8** — activation-allowance calibration: record the measured one- and
  two-image peaks and adjust `ACTIVATION_WEIGHT_FRACTION` /
  `MULTIMODAL_IMAGE_INCREMENT` if reality contradicts the estimate.

### Exploratory Bug Condition Checking

**Goal**: produce counterexamples on the UNFIXED code, and confirm or refute the
root-cause analysis. If refuted, re-hypothesize before implementing.

**Test Plan**: exercise today's `evaluate_fit`, today's publish gate, and today's
`manager.load` with fakes, asserting the *correct* behavior so the tests FAIL on
the current code and pin the exact defect.

**Test Cases**:

1. **Incident replay, fit math** — `evaluate_fit({'gpu_memory_utilization': 0.4},
   6.5 GiB, ['arm64_jp6'])` must report `fits = False` (will fail on unfixed code:
   it returns `fits = True` with `required_bytes = 7.5 GiB`).
2. **Co-tenancy hazard** — `util = 0.9` on `arm64_jp6` must not be reported as
   fitting, and no message may advise raising the fraction above 0.80 (will fail).
3. **Per-arch escape** — a record failing `arm64_jp6` while passing `arm64_jp7`
   must be refused with 422 (will fail: publish proceeds with `warnings`).
4. **Multimodal default** — `manager.load` with staged args omitting
   `limit_mm_per_prompt` must leave the engine args without the key (will fail:
   the recorded args contain `{"image": 2}`).
5. **Preflight absence** — with an injected reading of 3 GB available, `load` must
   refuse before calling the engine factory (will fail: no check exists, the
   factory is called).
6. **Starvation** — two consecutive failing loads with injected readings that do
   not recover must set the latch and refuse the second (will fail: no latch).
7. **Thin margin** — a fake engine reporting KV bytes below the floor must produce
   a WARNING (will fail: only the READY INFO line exists).
8. **Symptom classification** — an NVML-assert reason and a KV-cache reason must
   carry different category tokens (will fail: both are raw reasons).
9. **Edge case, two-image request** — a reference-image request against a model
   with `limit_mm_per_prompt.image = 1` must raise `GenerationError` (may fail
   differently today: the engine is invoked and fails deeper, or silently profiles
   for 2 because of the setdefault).

**Expected Counterexamples**: the fit verdict disagreeing with the device's
measured remainder by ~12 GiB; the publish gate admitting a JP6-infeasible
record; the injected 2-image default in the recorded engine args; the engine
factory being called with 3 GB of available memory. Possible causes if a case does
not fail as predicted: a different default resolution order in `parse_repository`,
or an engine-arg shape this design assumed wrongly — in which case re-hypothesize
before writing the fix.

### Fix Checking

**Goal**: for all inputs where the bug condition holds, the fixed pipeline
produces the expected behavior.

```
FOR ALL X WHERE isBugCondition(X) DO
  verdict := FitCheck'(X)
  ASSERT verdict.fits = FALSE
  ASSERT verdict.message NAMES weights, activation (as an estimate), KV floor,
                               budget, co-tenancy reservation, cap
  ASSERT verdict.remediation ORDERS demand-reduction BEFORE raising util
  ASSERT NOT (verdict.remediation SUGGESTS util > fraction_cap(X.arch))
END FOR

FOR ALL X WHERE isPerArchEscape(X) DO
  ASSERT Publish'(X) = 422 for X.arch   OR   (skip_fit_check AND audited)
END FOR

FOR ALL X WHERE isUnbudgetedMultimodal(X) DO
  ASSERT StagedArgs'(X) has no injected limit_mm_per_prompt
  ASSERT EffectiveLimit'(X) = authored value (default 1)
END FOR

FOR ALL (args, reading) WHERE required(args) > min(reading.available,
                                                  util * reading.total) DO
  ASSERT Load'(args) refuses BEFORE engine construction
  ASSERT reason STARTS WITH "preflight-refused:"
  ASSERT reason NAMES reading.available, required(args), the setting to change
  ASSERT Prep'(reason) = (LOAD_PREFLIGHT_REFUSED, exit 0, no unload->reload)
END FOR

FOR ALL X WHERE isStrandedCascade(X) DO
  ASSERT latch_set(X) AND retry_refused(X) with the two readings named  // host
  ASSERT device_free_after(X) >= device_free_before(X) - epsilon        // [HARDWARE] H4
END FOR
```

### Preservation Checking

**Goal**: for all inputs where the bug condition does NOT hold, the fixed
pipeline produces the same result as the original.

```
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT FitCheck(X).fits    = FitCheck'(X).fits          // verdict unchanged
  ASSERT FitCheck(X).status  = FitCheck'(X).status        // passed/unverified
  ASSERT Publish(X)          = Publish'(X)                // status, annotation, audit
  ASSERT StagedArgs(X)|5keys = StagedArgs'(X)|5keys       // model.json byte-identical
  ASSERT PrepExit(X)         = PrepExit'(X)               // LOAD_OK/UNREACHABLE/HTTP_ERROR
  ASSERT Reconciler(X)       = Reconciler'(X)             // untouched module
  ASSERT JP7Load(X)          = JP7Load'(X)                // [HARDWARE] H6
  ASSERT OnnxLoad(X)         = OnnxLoad'(X)               // [HARDWARE] H2
END FOR
```

**Testing Approach**: property-based testing for preservation, because the
preserved surface is a wide input space (arbitrary utilizations, weights, arch
sets, engine-config overlays) where hand-picked examples miss edge cases, and
because the sibling suite already establishes the generators
(`engine_configurations()`, `estimates()`, `_architecture_sets`) — reusing them
keeps the two specs' guarantees comparable.

**Test Plan**: observe behavior on the UNFIXED code first for the preserved paths
(a fitting record, an unverified estimate, a JP7 record, a text-only model, a
`LOAD_OK` prep run, a `LOAD_UNREACHABLE` prep run), record it, then assert the
fixed code reproduces it.

**Test Cases**:

1. **Fitting record** — a record that fits under both models keeps `fits = True`,
   status `passed`, identical `budget_bytes`, and publishes with the same response
   and audit events.
2. **Unverified estimate** — registration, update and publish all still proceed
   with `unverified` and no findings (3.2); `estimate_weights` still never raises.
3. **Five existing engine settings** — resolution, validation ranges, fail-closed
   unknown keys, and verbatim propagation into `model.json` unchanged (3.3); the
   new key is additive and defaults to `{"image": 1}`.
4. **Prep exit codes** — `LOAD_OK` → 0, `LOAD_UNREACHABLE` → 1 with its
   diagnostic, `LOAD_HTTP_ERROR` → 1; the KV-OOM unload→reload recovery still
   fires exactly once for a genuine KV-OOM reason (3.8).
5. **Reconciler** — its module is untouched; its test suite runs unchanged.
6. **Healthy load** — a fake engine with ample KV produces no thin-margin warning
   and the byte-identical READY log line.
7. **Two-image model** — a model authored with `limit_mm_per_prompt.image = 2`
   builds the two-image prompt exactly as today (3.9).

### Unit Tests

- `activation_allowance` / `fraction_cap` / `images_per_prompt` edge cases:
  zero and tiny weights, `Decimal` utilizations, malformed
  `limit_mm_per_prompt`, unknown architecture.
- Message composition: every term present, remediation ordering, the never-lower
  invariant, and the cap sentence appearing only when relevant.
- Publish gate branches: any-arch fail → 422; any-arch fail + `skip_fit_check` →
  `overridden` + audit; all fit → `passed`; soft warning → `warnings`;
  unverified → proceed.
- `read_memory` parsing: normal `/proc/meminfo`, missing keys, garbage, empty.
- `estimate_weights_on_disk`: local dir, HF cache layout, absent, unreadable.
- Failure classifier: one token per reason class, original text preserved,
  precedence of `preflight-refused` over the KV markers in the prep.
- Prep exit-code mapping for all four classifications.

### Property-Based Tests

- Property 1/3 over generated (utilization, weights, arch set, images) — the
  verdict equals `A ∧ B` and the message invariants hold for every generated
  failing finding.
- Property 2 preservation over the same generators, comparing against the
  recorded pre-fix behavior for non-bug inputs.
- Property 5/6 over generated memory readings and sequences of load attempts —
  refuse iff the requirement exceeds the measured minimum; the latch is set iff
  memory did not recover.
- Property 8 over a grid of (arch, weights, util, images) — the portal and device
  models agree on required bytes and the budget verdict.
- Property 4 over generated staged-args dictionaries — no key is ever injected
  that the authored configuration did not contain.

### Integration Tests

- Host: register → update engine configuration → package → publish, end to end
  under moto, asserting the authored `limit_mm_per_prompt` reaches the generated
  `model.json` verbatim and that a JP6-infeasible configuration is refused at
  publish with the per-arch findings.
- Host: stage a repository with `vllm_model_prep`, drive `request_load` against a
  crafted 409 preflight-refusal body, and assert the classification, the log line
  and exit 0.
- Device **[HARDWARE]**: H1–H6 above, run as USER ACTION tasks on
  `ryanorinagxdevkithomelabjp622` after the JP6 component build, with the ONNX
  models exercised in the same window.

## Honesty Guard

**Claims that can ONLY be established on hardware — no host test may assert
them, and no task may claim them as done without a device run:**

- That the fixed component actually loads `qwen2-5-vl-7b-instruct-awq` on JP6, and
  the resulting KV remainder / concurrency **[H1]**.
- The two-image activation peak, and therefore the true magnitude of defect 1.4's
  contribution. Everything about it in this design is a hypothesis about
  magnitude, deliberately conservative **[H8]**.
- Whether `ACTIVATION_WEIGHT_FRACTION = 0.75` and
  `MULTIMODAL_IMAGE_INCREMENT = 1.0` are right. They are estimates from a single
  measured point; the design prefers a conservative allowance plus the device-side
  truth check over false precision **[H8]**.
- That `CO_TENANCY_RESERVATION_BYTES['arm64_jp7'] = 8 GiB` resembles thor1's
  reality. It is an unmeasured placeholder chosen where it cannot flip a JP7
  verdict at the utilizations in use **[H6]**.
- That memory is actually reclaimed (or not) across the NVML-assert path, and the
  assert's root cause **[H4, H7]**. Host tests only prove the *decision logic*
  over injected readings.
- That the three ONNX GPU models keep serving on GPU with an unchanged footprint
  **[H2]**.
- That the runtime server stays responsive during a refusal and that the
  Greengrass deployment succeeds with the model FAILED-with-reason **[H3]**.
- That JP7 is unaffected **[H6]**.
- Jetson unified-memory accounting itself: host tests inject `/proc/meminfo`
  text and never reproduce how vLLM charges `non_torch_memory` on a unified-memory
  device.

**What IS host-provable, with no GPU and no vLLM wheel:** the fit-check math and
message content; the publish gate's fail/pass/override/unverified branches; the
engine-arg authoring, validation, resolution, packaging and staging path; the
preflight and starvation **decision logic** over injected memory readings; the
weights-on-disk probe over tmp trees; the failure classifier; the prep's
classification and exit codes; and the portal/device constant-and-formula parity.

**Device access for further read-only evidence** (the device is HEALTHY on 1.0.59
serving the model — it must be left that way; no state changes, no deployments,
no container restarts during design/investigation):
`sshpass -p lookout ssh -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -p 9998 aws@ryan.120v.ac`,
sudo password `lookout`.

**The 1.0.61 image question.** Re-pulling
`164152369890.dkr.ecr.us-east-1.amazonaws.com/dda/flask-app:1.0.61` **on the build
host (never onto the device)** and running `grep -c limit_mm_per_prompt
/vllm_runtime/manager.py` plus a `pip freeze` diff against 1.0.59 would settle
both open 1.0.61 questions definitively. **This design does not depend on that
answer**: removing the unbudgeted default and making the sizing model sound is
correct either way, and if the diff reveals a second moved variable it changes
the *explanation*, not the fix. The inspection belongs in the task plan (as a
read-only evidence task) rather than being guessed at here — and if it shows a
floated dependency also moved, this design must be revisited before H1 is
declared a success.

## Explicitly NOT changed

Inherited from bugfix.md and binding here:

- `src/backend/vllm_runtime/reconciler.py` and its wiring in `src/backend/app.py`
  (owned by `vllm-model-reload-after-backend-restart`).
- `src/backend/vllm_runtime/repository.py`, the runtime server's routes and status
  mappings, the tombstone contract (`.dda_explicit_unload`),
  `ModelState.UNLOADED`, and the feature-config / 409-category status maps. This
  spec is a consumer of the existing load/unload contract; the preflight refusal
  travels as a normal `FAILED(reason)`.
- `src/backend/Dockerfile.jp7`, the JP7 from-source vLLM build
  (`VLLM_VERSION=v0.11.2`), `TRITON_PTXAS_PATH`, and every JP7 engine default.
- `src/backend/Dockerfile.jp6` and its pins (`vllm==0.9.3+cu126`, `torch==2.8.0`,
  `VLLM_USE_V1=0`) — the pins did not move between 1.0.59 and 1.0.61 and are not
  the defect.
- The JP5 (`VLLM_ENABLE=0`) and x86 images' vLLM-free inertness.
- The ONNX/Triton vision path: `model_convertor.py`, `inference_runtimes.py`, the
  vision model recipes, and the three JP6 ONNX model components.
- `vllm-jp7-engine-cuda-init`'s territory: the `cudaErrorDevicesUnavailable`
  nvargus/Argus finding, its NVIDIA bug-report draft, and the
  `VLLM_WORKER_MULTIPROC_METHOD` disposition.
- `model-gpu-fallback-visibility`'s GPU status surfaces — not duplicated, not
  extended (Decision 6 states the disjointness).
- The awscrt "Continuation ref count has gone negative" class and the thor1
  cold-first-generate finding.
- Greengrass deployment/revision machinery, ShadowManager sync, and the portal
  build fleet.
- Every preservation-tracked file (`src/docker-compose.yaml`,
  `src/backend/Dockerfile*`, `src/frontend/Dockerfile`, `src/edgemlsdk/Dockerfile`,
  `src/backend/requirements.txt`, the recipe variants,
  `station_install/setup_station.sh`) — no change is expected; if one becomes
  necessary, its baseline is rebaselined in the same change and the preservation
  suite is run in the flask-app container before any build starts.
- `_reclaim_gpu_memory`'s CUDA-init invariant: nothing in this design may
  initialize CUDA in the parent backend process.

## Rollout shape

`.kiro/steering/builds.md` is binding: **one component build at a time**, **never
a portal deploy while a build is running**, JP6 is the verification target, and
on-hardware verification comes before "done".

**Leg 0 — evidence (no build, no deploy, no device change).** Re-pull the 1.0.61
image on the build host and settle the `limit_mm_per_prompt` grep and the
`pip freeze` diff. Read-only device evidence only if needed.

**Leg A — portal (fit check, publish gate, engine defaults, frontend types).**
Ships by portal deploy; no component build involved. Host tests (Properties
1–4, 8 and the portal half of preservation) must be green first. Because it
changes only publish/authoring behavior, it cannot disturb the serving device
(3.11).

**Leg B — device (`manager.py`, new `memory_budget.py`,
`vllm_model_prep.py`).** Ships by an `aws.edgeml.dda.LocalServer.arm64JP6`
component build. **This is the leg that restores service** — with the
unconditional 2-image default gone, the already-published `model.json` loads as
it did on 1.0.59, with no re-package and no re-publish.

**Sequencing (strict):**

1. Leg 0 evidence. If it contradicts the root-cause analysis, revisit this design
   before building anything.
2. Leg A host tests green → **portal deploy fully finishes**.
3. Move `edge-cv-portal/infrastructure/cdk.out` aside; check `git status` against
   the preservation-tracked files; run the two out-of-scope guard tests and
   confirm they are green (the portal deploy in step 2 regenerates `cdk.out`, which
   is the classic cause of a late gate failure after a ~1h compile).
4. Confirm no build is running (`pgrep -af "gdk component build"`,
   `pgrep -af "build-custom.sh"`), then start the **single** JP6 build with
   `gdk-config.json` set to `aws.edgeml.dda.LocalServer.arm64JP6`, logging to
   `.gdk_build_jp6.log`. No portal deploy from here until it finishes. Restore
   `gdk-config.json` afterwards.
5. Deploy the new LocalServer to `ryanorinagxdevkithomelabjp622` — the first
   deployment that deliberately disturbs the healthy 1.0.59 device — and run
   **H1, H2, H3, H5** (and H4 if it can be induced safely). If H1 or H2 fails,
   roll back to 1.0.59 (the known-good revision pinning it is on record) before
   iterating.
6. **H6** on thor1 (`LocalServer.arm64JP7`) to confirm JP7 is unaffected. JP7 code
   is untouched, so this is a verification step, not a build.
7. Only then: decide whether to re-author and **re-publish** the model. A
   re-publish is needed only if the operator wants a different authored
   configuration (an explicit `limit_mm_per_prompt`, a different
   `max_model_len`, or a `util` re-authored within the 0.80 cap) — any engine-arg
   change requires re-packaging and re-publishing the model component, and the
   corrected gate will then evaluate it. **Not required to fix the regression.**
8. **H7** (NVML-assert determination) and **H8** (allowance calibration) run after
   the device is healthy again, on the fixed component, and may adjust the
   constants in a follow-up change through the same two legs.

**Rollback posture.** Leg A is revertible by a portal deploy. Leg B is revertible
by pinning the previous LocalServer revision, exactly as revision
`8b697b31-f5cf-4d09-8a58-70b3cc0afb96` restored 1.0.59 during the incident.

## Open questions (recorded, not guessed)

1. **Why the NVML allocator INTERNAL ASSERT occurs** — determination path in
   Decision 6, **[H7]**. Reported distinguishably in the meantime; no cause
   invented.
2. **The 1.0.61 dependency set and its `limit_mm_per_prompt` grep** — Leg 0. The
   fix does not depend on the answer; the *explanation* might.
3. **The two-image activation peak** — **[H8]**, still unmeasured; the
   per-additional-*image* coefficient stays a conservative placeholder.
   **Partially answered 2026-08-19 for the *unit* term**: `{'image': 1, 'video':
   0}` → 2.47 GiB and `{'image': 1}` (video unbound) → 4.93 GiB at
   `util = 0.55`, i.e. the 2× per-unit ratio is right and
   `ACTIVATION_WEIGHT_FRACTION` is ~2× high (implied ≈0.375 of weights). Not
   applied: the constant is mirrored in the device module and both legs must move
   in one change, which needs a JP6 component build (task 14).
4. **JP7 co-tenancy reality on thor1** — unmeasured; the 8 GiB reservation is a
   placeholder that cannot flip a JP7 verdict at the utilizations in use.
5. **Whether `RECLAIM_TOLERANCE_BYTES = 0.5 GiB` and
   `THIN_MARGIN_CONCURRENCY = 2.0x` are the right thresholds** — proposed from the
   incident numbers (the 8.34 GiB swing and the 2.95x/0.00x observations);
   confirm on hardware and adjust with evidence.
