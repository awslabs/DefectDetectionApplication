# DDA Imaging Layer Empty — Bugfix Design

## Overview

The ComputeStack's `ImagingLayer` (`edge-cv-portal/infrastructure/lib/compute-stack.ts`, ~line 1805) ships Pillow to the three DDA labeling functions (`DdaLabelingWorker`, `DdaLabelingHandler` — which runs the async Prompt_Tuning_Preview executor — and `DdaAutolabelWorker`). Its CDK asset is the raw `backend/layers/imaging/` directory, which only contains a Pillow install if `backend/layers/imaging/build.sh` was run manually on the deploy host beforehand. `deploy-infrastructure.sh` never runs it; on this host it hadn't been run at deploy time (2026-09-06), so the layer shipped empty and every Pillow code path in the labeling functions failed at runtime with `No module named 'PIL'` — surfacing as per-image "Model error" badges in Prompt Tuning Preview.

This is the identical bug already fixed for the SyntheticDataStack's `SyntheticImagingLayer` (same source directory) in the `synthetic-imaging-layer-empty` spec, cherry-picked onto this branch as commit ea528a3. The fix mirrors that pattern exactly: replace the raw `lambda.Code.fromAsset(...)` with **CDK asset bundling** — a Docker bundling image (`lambda.Runtime.PYTHON_3_11.bundlingImage`) with a **local bundling fallback** (`local.tryBundle`) that runs the same manylinux-targeted pip install on the host that `build.sh` performs. Synth then always produces a populated layer asset containing `python/PIL`, and host state can never determine deployed layer content again. `build.sh` stays as the manual/standalone build path.

Because both stacks bundle the same source directory with identical bundling options, CDK assigns both layers the same asset hash, which also restores the currently-failing pinned cross-stack test ('SyntheticImagingLayer is unchanged and still bundles its own copy of the imaging asset' — `Content.S3Key` equality) without editing it. Scope is strictly `compute-stack.ts`'s `ImagingLayer`; the synthetic stack is already fixed and untouched.

## Glossary

- **Bug_Condition (C)**: The ComputeStack stages the `ImagingLayer` asset as a verbatim copy of the raw source directory (no synth-time bundling) — build tooling staged as layer content, `python/PIL` present only by host accident, and (post-cherry-pick) an asset key diverging from the synthetic stack's bundled asset.
- **Property (P)**: The synthesized `ImagingLayer` asset is synth-time bundling output: it always contains `python/PIL` (Pillow from `requirements.txt`, including the native manylinux extension), contains no staged build tooling, and is the identical asset the SyntheticDataStack's `SyntheticImagingLayer` bundles.
- **Preservation**: Attach sites and function configuration of the three DDA labeling functions, the `ImagingLayer` metadata (description, compatible runtimes — pinned by existing tests), all other compute-stack assets (e.g. `SharedLayer`), the untouched `synthetic-data-stack.ts`, the existing test suite, and the manual `build.sh` workflow.
- **ImagingLayer**: The `lambda.LayerVersion` construct id `'ImagingLayer'` in `compute-stack.ts` bundling Pillow 10.4.0 (`requirements.txt`: `Pillow==10.4.0`) for the DDA labeling functions.
- **SyntheticImagingLayer**: The already-fixed sibling `lambda.LayerVersion` in `synthetic-data-stack.ts` (lines ~185-236) bundling the same `backend/layers/imaging` directory — the pattern this fix mirrors verbatim.
- **`_import_pillow_image`**: The lazy Pillow import in `backend/layers/shared/python/dda_llm_image.py` (~line 320; module contract: no Pillow at import time), called by the Image_Downscaler resize path and the Pillow header fallback — why the ImportError surfaces per-image inside the model-call path instead of at cold start.
- **Pinned cross-stack test**: `llm-model-token-and-image-sizing-infra.test.ts` ~line 299, asserting the synthetic layer's `Content.S3Key` equals the compute layer's — currently the suite's only failure (1 failed / 124 passed).
- **Local bundling fallback**: CDK's `ILocalBundling.tryBundle` hook — runs the bundling command on the host; returning `false` makes CDK fall back to the Docker bundling image.

## Bug Details

### Bug Condition

The bug manifests whenever the ComputeStack is synthesized: `lambda.Code.fromAsset` on the plain directory stages `backend/layers/imaging/` verbatim, so the layer content is a function of host state. On an unbuilt host the staged zip holds only `build.sh` and `requirements.txt`; CloudFormation deploys it without complaint and the labeling functions fail at runtime on their first Pillow import. On a built host the layer "works" only by accident — and the staged asset still wrongly includes the build tooling and still diverges from the synthetic stack's bundled asset.

**Host-state caveat (critical for the exploration test)**: `build.sh` has been run on this host as an operational unblock, so `backend/layers/imaging/python/` currently EXISTS. A test asserting merely "staged asset lacks PIL" would pass on the unfixed code here and prove nothing. The bug condition must therefore be checked structurally — raw-copy markers and asset identity — which is host-state independent.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type SynthesizedComputeImagingLayerAsset
  OUTPUT: boolean

  // Raw-directory staging: the staged asset is whatever the host's
  // backend/layers/imaging happens to contain. Structural signatures,
  // host-state independent:
  RETURN contains(input.stagedAssetDir, "build.sh")            // build tooling staged as layer content
         OR contains(input.stagedAssetDir, "requirements.txt")
         OR NOT exists(input.stagedAssetDir + "/python/PIL")   // unbuilt host => empty layer
END FUNCTION
```

Corollary (given the cherry-picked synthetic fix): on unfixed code the compute layer's `Content.S3Key` diverges from the synthetic `SyntheticImagingLayer`'s — the pinned cross-stack equality failure is this bug condition made visible at the template level. (Equivalently at runtime: any invocation of a DDA labeling function that reaches `_import_pillow_image()` while the attached imaging layer lacks `python/PIL`.)

### Examples

- Prompt Tuning Preview run (user screenshot): run status "completed", but every per-image result shows a red "Model error" badge with `model error: No module named 'PIL'` and "dimensions unavailable" — the executor catches and reports the lazy-import failure per image.
- Deployed layer content (verified on this host): cdk.out asset `98f3a4d8ab2dddc0be7b06f4df3eb8ff53523dd9b4c7c616e31f105fc9d3ce65` from the 2026-09-06 deploy (`deploy-infra-llm-token-image-sizing-20260906T013238Z.out`) contains exactly two files — `build.sh`, `requirements.txt` — no `python/`.
- Fresh clone + `deploy-infrastructure.sh` (which never runs `build.sh`) reproduces the empty layer deterministically.
- Edge case: this host today — `build.sh` has been run, `python/` exists, so a raw staging happens to include PIL. The layer works by accident, but the structural defect remains (build tooling staged, asset ≠ the synthetic bundled asset, next fresh host regresses). This is exactly why the fix and its exploration test target synth-time structure, not host outcome.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- The single `ImagingLayer` LayerVersion must remain attached to exactly `DdaLabelingWorker`, `DdaLabelingHandler`, and `DdaAutolabelWorker`, as the second layer after `SharedLayer` in each case (one shared Pillow build — the byte-identical-downscale precondition of llm-model-token-and-image-sizing Req 6.6).
- The three functions' configuration must not change: runtime python3.11; handlers `dda_labeling_worker.handler` / `dda_labeling.handler` / `dda_autolabel_worker.handler`; timeouts 900/900/300 s; MemorySize 2048 each; environment wiring (including LLM_MODEL_TOKEN_LIMITS / LLM_MODEL_IMAGE_LIMITS on handler+autolabel worker but not on the labeling worker).
- The `ImagingLayer`'s description string `Pillow imaging layer for DDA labeling mask rendering (built by backend/layers/imaging/build.sh)` and `CompatibleRuntimes: ['python3.11']` are pinned byte-exact by a currently-passing test in `llm-model-token-and-image-sizing-infra.test.ts` and MUST NOT change. The description's build.sh mention becomes provenance documentation (build.sh still defines the manual path and the wheel targeting the bundling mirrors); only the adjacent code comment is rewritten. Layer metadata is not part of the code asset, so keeping it has no effect on asset hashes.
- `SharedLayer` (and every other compute-stack asset) must continue to be staged exactly as before — bundling applies to the imaging layer only.
- `synthetic-data-stack.ts` must not be touched: the cherry-picked bundling stays exactly as-is.
- All 124 currently-passing infrastructure tests must stay green with no existing test file edited; the pinned cross-stack S3Key-equality test must return to green UNMODIFIED (restoring the pre-existing suite to 125/125).
- `backend/layers/imaging/build.sh` must continue to work for manual/standalone layer builds (file unchanged).

**Scope:**
All parts of the ComputeStack other than the `ImagingLayer` asset production are out of scope and must be equivalent in the synthesized template (modulo the imaging layer's asset key). No handler code, no other stack, no other layer source, and no test file is touched by the fix itself (the spec only ADDS a new test file).

## Hypothesized Root Cause

Confirmed by live evidence (no re-derivation needed):

1. **Convention without enforcement**: The compute `ImagingLayer` copied the "run `build.sh` before deploy" convention (its own comment says "same convention as the jwt layer"), but nothing in synth or deploy verifies `python/` exists. `deploy-infrastructure.sh` runs `cdk deploy --all` and never runs `build.sh`.
2. **`python/` is not committed**: Unlike `layers/shared` (whose `python/` is tracked in git), the imaging layer's `python/` only exists after a manual build, so any fresh checkout deploys an empty layer.
3. **No content assertion in tests**: `llm-model-token-and-image-sizing-infra.test.ts` pins the layer's existence, description, runtimes, and attach sites — but not the asset content, so an empty layer synthesizes, tests green, and deploys cleanly.
4. **Half-applied precedent**: Commit ea528a3 fixed the same defect on the synthetic stack only; the compute stack kept the raw asset. That divergence is what currently fails the pinned cross-stack asset-equality test.

## Correctness Properties

Property 1: Bug Condition - Synthesized Compute ImagingLayer Asset Is Bundled Pillow

_For any_ synthesis of the ComputeStack (isBugCondition on the unfixed code: the staged `ImagingLayer` asset is the raw source directory — build tooling staged, PIL present only by host accident), the fixed stack SHALL stage an `ImagingLayer` asset that is synth-time bundling output: it contains `python/PIL` with the Pillow package installed from `requirements.txt` (including the native manylinux `_imaging*.so` extension), contains no `build.sh` or `requirements.txt` at the asset root, and carries the identical asset key as the SyntheticDataStack's bundled `SyntheticImagingLayer` (equal `Content.S3Key`), so the deployed layer provides PIL to the DDA labeling functions regardless of host state.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation - Attach Sites, Layer Metadata, Sibling Assets, Suite, and Manual Build Unchanged

_For any_ synthesis of the ComputeStack where the bug condition does not apply (everything other than the `ImagingLayer` asset production), the fixed stack SHALL produce the same result as the original stack: the single `ImagingLayer` attached as second layer after `SharedLayer` to exactly `DdaLabelingWorker`, `DdaLabelingHandler`, and `DdaAutolabelWorker` with unchanged function configuration; unchanged `ImagingLayer` description and compatible runtimes; `SharedLayer` staged verbatim from `backend/layers/shared`; `synthetic-data-stack.ts` untouched; all 124 currently-passing infrastructure tests green with no existing test file edited; and `build.sh` SHALL continue to produce a `python/` directory with Pillow when run manually.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5**

## Fix Implementation

### Changes Required

**File**: `edge-cv-portal/infrastructure/lib/compute-stack.ts`

**Construct**: `ImagingLayer` (`lambda.LayerVersion` code asset, ~line 1805)

**Specific Changes**:
1. **Bundled asset — mirror `synthetic-data-stack.ts` verbatim**: Replace `lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/imaging'))` with the bundled form used by `SyntheticImagingLayer`:
   - extract `const imagingLayerSourceDir = path.join(__dirname, '../../backend/layers/imaging');`
   - `bundling.image: lambda.Runtime.PYTHON_3_11.bundlingImage`
   - `bundling.command: ['bash', '-c', 'pip install -r requirements.txt -t /asset-output/python']` — byte-identical strings to the synthetic stack's (this matters for asset-hash equality, below)
2. **Local bundling fallback** (`bundling.local.tryBundle`): run pip on the host with the same wheel targeting as `build.sh` (`--platform manylinux2014_x86_64 --implementation cp --python-version 3.11 --only-binary=:all:`) into `<outputDir>/python`, `return true` on success, `return false` on any failure so CDK falls back to the Docker image — the same `execSync` body as the synthetic stack. Extend the existing `child_process` import (line 21, currently `{ execFileSync }`) to also import `execSync`.
3. **Why the pinned test passes unmodified**: For bundled assets CDK computes the asset hash from the SOURCE fingerprint plus the serialized bundling options (function values like `tryBundle` are dropped by JSON serialization). Both layers bundle the same `backend/layers/imaging` directory; with byte-identical `image` and `command`, both get the same asset hash, CDK stages that asset once per assembly, and both `Content.S3Key`s reference it — restoring `expect(synthetic S3Key).toBe(compute S3Key)` by construction, independent of pip output byte-stability. Only if this equality empirically fails (it should not — the mechanism is deterministic) may the pinned test be updated, with explicit justification recorded; treat that as a last resort.
4. **Comment update, metadata frozen**: Rewrite the layer's code comment to document synth-time bundling (mirroring the synthetic stack's BUGFIX comment, citing this spec) and note `build.sh` remains for manual builds. The `description` and `compatibleRuntimes` values stay byte-identical — both are pinned by a currently-passing test (Preservation).
5. **Keep `build.sh`**: unchanged — still the documented manual/standalone build path whose wheel-targeting flags the local bundling mirrors. (A pre-existing `python/` in the source dir remains irrelevant post-fix: bundling output, not the raw directory, becomes the asset. It only influences the source fingerprint, identically for both stacks.)

No changes to `synthetic-data-stack.ts`, `dda_llm_image.py`, other layers, other stacks, or any existing test file.

## Testing Strategy

### Validation Approach

Two-phase: first a synth-level exploration test that demonstrates the structural defect on unfixed code (host-state independent — see the caveat in Bug Details), then fix verification plus preservation checks, followed by a compute-stack redeploy and live verification that a populated bundled layer version is attached. The live incident is already mitigated by an operational deploy (manual `build.sh` + redeploy), so runtime symptom checks are non-diagnostic for this fix; the synth-level assertions are the proof that host-state dependence is gone.

### Exploratory Bug Condition Checking

**Goal**: Surface the counterexample on UNFIXED code — the staged `ImagingLayer` asset is a raw directory copy, not bundling output — confirming the (already live-confirmed) root cause at the synth level, in a way host state cannot fool.

**Test Plan**: A jest test (`edge-cv-portal/infrastructure/test/dda-imaging-layer-empty.test.ts`) synthesizes the ComputeStack and SyntheticDataStack into one cloud assembly (`app.synth()`; stack construction mirrors the beforeAll of `llm-model-token-and-image-sizing-infra.test.ts`, staged-asset resolution via `Content.S3Key` → `<assemblyDir>/asset.<hash>` mirrors `synthetic-imaging-layer-empty.test.ts`; synthesize once in beforeAll with a generous timeout — ComputeStack synth stages many assets and runs the quick-setup packaging script). Run on the UNFIXED stack to observe the failure.

**Test Cases** (one test, three assertion groups — all must hold post-fix):
1. **No raw-copy markers**: the staged `ImagingLayer` asset contains NO `build.sh` and NO `requirements.txt` at its root (bundling output is `python/` only) — fails on unfixed code regardless of host state
2. **Populated Pillow install**: `<asset>/python/PIL` exists with real module content (`Image.py`, `ImageDraw.py`) and ≥1 native `_imaging*.so` manylinux extension — fails on unfixed code on any unbuilt host (passes by host accident here today, which is why assertion 1 exists)
3. **Cross-stack asset identity**: the compute `ImagingLayer`'s `Content.S3Key` equals the synthetic `SyntheticImagingLayer`'s — fails on unfixed code (raw source hash vs bundled hash)

**Expected Counterexamples** (on this host):
- Staged compute imaging asset root listing = `['build.sh', 'python/...', 'requirements.txt']` — raw copy markers present (on a fresh host: exactly `['build.sh', 'requirements.txt']`, the deployed-empty incident).
- Compute `Content.S3Key` ≠ synthetic `Content.S3Key` — the same divergence the pinned test currently fails on.

### Fix Checking

**Goal**: Verify that for all syntheses, the fixed stack stages a bundled, populated imaging layer asset.

**Pseudocode:**
```
FOR ALL synth WHERE isBugCondition would hold on the unfixed stack DO
  asset := stageImagingLayerAsset_fixed(synth)
  ASSERT exists(asset + "/python/PIL") AND NOT contains(asset, "build.sh")
         AND asset.contentS3Key = syntheticImagingLayerAsset(synth).contentS3Key
END FOR
```

The CDK synthesis is deterministic, so the exhaustive synth-level assertion (same test as exploration, now passing) is the all-inputs guarantee. Live fix checking on top: redeploy the ComputeStack, confirm a NEW imaging layer version is attached to the three DDA labeling functions, and inspect the deployed layer content for `python/PIL` (produced by bundling, not by host state).

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed stack behaves identically to the original.

**Pseudocode:**
```
FOR ALL synth DO
  ASSERT layers(DdaLabelingWorker)  = [SharedLayer, ImagingLayer]   // same single LayerVersion Ref
  ASSERT layers(DdaLabelingHandler) = [SharedLayer, ImagingLayer]
  ASSERT layers(DdaAutolabelWorker) = [SharedLayer, ImagingLayer]
  ASSERT functionConfig_fixed(synth) = functionConfig_original(synth) // runtime/handler/timeout/memory
  ASSERT imagingLayerMetadata_fixed = imagingLayerMetadata_original   // description, runtimes
  ASSERT sharedLayerAsset_fixed(synth) = verbatim copy of backend/layers/shared
END FOR
```

**Testing Approach**: The synthesized template and staged assets are deterministic functions of the source tree, so exhaustive template/asset assertions provide the universal guarantee (same reasoning as the precedent `synthetic-imaging-layer-empty` tests).

**Test Plan**: Observe on UNFIXED code (observation-first) that the three functions carry exactly `[SharedLayer, ImagingLayer]` with the configurations above, that the layer metadata matches the pinned strings, and that `SharedLayer` stages verbatim; encode those observations as assertions in the same test file; verify they pass before AND after the fix.

**Test Cases**:
1. **Attach sites**: the three DDA labeling functions each carry exactly two layers, second Ref = the stack's single `ImagingLayer` LayerVersion (same Ref across all three)
2. **Function configuration**: handlers, runtime python3.11, timeouts 900/900/300, MemorySize 2048 each
3. **Layer metadata**: `ImagingLayer` description and `CompatibleRuntimes` byte-equal to the pinned strings
4. **Shared layer verbatim**: staged `SharedLayer` asset file set equals the `backend/layers/shared` source file set
5. **Full suite**: all 125 pre-existing infrastructure tests green (124 currently passing + the pinned cross-stack test restored, unmodified)

### Unit Tests

- Synth-level staged-asset content and asset-identity assertions (exploration/fix test above)
- Attach-site, function-configuration, and layer-metadata template assertions (preservation)

### Property-Based Tests

- The synthesized template and staged assets are deterministic functions of the source tree; the exhaustive template/asset assertions quantify over every attach site, every function property, and the full asset content, serving as the property check (consistent with the precedent spec and prior infra bugfix specs in this repo)

### Integration Tests

- Redeploy of `EdgeCVPortalComputeStack` (account 164152369890, us-east-1), honoring `.kiro/steering/builds.md` (no portal deploy while a component build runs; handle `cdk.out` drift-guard baselines afterwards)
- Live verification: NEW `ImagingLayer` version attached to the three DDA labeling functions; deployed layer zip contains `python/PIL` and no build tooling; a Prompt Tuning Preview spot check may additionally confirm per-image dimensions render without model errors (non-diagnostic post-mitigation, but confirms end-to-end health)
