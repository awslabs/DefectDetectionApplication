# Synthetic Imaging Layer Empty — Bugfix Design

## Overview

The `SyntheticImagingLayer` Lambda layer deployed for `dda-synthetic-data-handler` is empty: its CDK asset is the raw `backend/layers/imaging/` directory, which holds only `build.sh` and `requirements.txt`. The convention (inherited from the sibling jwt layer) is that `build.sh` pip-installs Pillow into `python/` before `cdk deploy` — but it was never run, `cdk synth` happily staged the empty directory, and the deploy succeeded. The failure only surfaced at runtime: `No module named 'PIL'` when the stability inpaint generation path calls `_render_mask_png` in `synthetic_data.py`.

The fix moves the imaging layer from "copy the directory and hope build.sh was run" to **CDK asset bundling**: `lambda.Code.fromAsset(..., { bundling: ... })` with a Docker bundling image (`lambda.Runtime.PYTHON_3_11.bundlingImage`) and a **local bundling fallback** that runs the same pip install on the host (with manylinux wheel targeting, matching `build.sh`). Synth then always produces a populated layer asset containing `python/PIL`, and an empty layer can never ship again. `build.sh` is kept working for manual/standalone layer builds.

## Glossary

- **Bug_Condition (C)**: The imaging layer asset directory contains no `python/PIL` content at synth time — synth/deploy succeed but the runtime PIL import fails.
- **Property (P)**: The synthesized imaging layer asset always contains `python/PIL`, so the deployed layer provides Pillow and the handler's PIL imports succeed.
- **Preservation**: The shared and JWT layer staging, the handler function configuration (three layers, runtime, memory, timeout, environment, role), the API routes, and the manual `build.sh` workflow must remain unchanged.
- **SyntheticImagingLayer**: The `lambda.LayerVersion` in `edge-cv-portal/infrastructure/lib/synthetic-data-stack.ts` bundling Pillow for the synthetic data handler.
- **`_render_mask_png`**: The function in `edge-cv-portal/backend/functions/synthetic_data.py` (~line 727) that imports `PIL.Image`/`PIL.ImageDraw` to synthesize inpaint masks; two more PIL imports exist at ~746 and ~999.
- **Local bundling fallback**: CDK's `ILocalBundling.tryBundle` hook — runs the bundling command on the host; if it fails, CDK falls back to the Docker image.

## Bug Details

### Bug Condition

The bug manifests when the SyntheticDataStack is synthesized while `backend/layers/imaging/` has no built `python/` directory. `lambda.Code.fromAsset` stages the directory verbatim, so the layer zip contains only `build.sh` and `requirements.txt`. CloudFormation deploys it without complaint, and the handler fails at runtime the first time a PIL import executes.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type SynthesizedImagingLayerAsset
  OUTPUT: boolean

  RETURN NOT exists(input.stagedAssetDir + "/python/PIL")
END FUNCTION
```

(Equivalently at runtime: any invocation of `dda-synthetic-data-handler` that reaches a `from PIL import ...` statement while the attached imaging layer lacks `python/PIL`.)

### Examples

- Generation session 6525c09c with `stability.stable-image-inpaint-v1:0`: generation worker reached `_render_mask_png`, `from PIL import Image, ImageDraw` raised `ModuleNotFoundError: No module named 'PIL'`, session recorded a generation failure and ended in `awaiting_review`.
- Deployed layer `arn:aws:lambda:us-east-1:164152369890:layer:SyntheticImagingLayer6E5D81DF:1` contains only `build.sh` and `requirements.txt` — no `python/` directory.
- Fresh clone + `cdk deploy` (without manually running `build.sh`) reproduces the empty layer deterministically.
- Edge case: a developer who *did* run `build.sh` gets a working layer — the bug is deploy-environment-dependent, which is exactly why it must be fixed at synth time.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- The shared layer (`SyntheticSharedLayer`) and JWT layer (`SyntheticJwtLayer`) must continue to be staged exactly as before: verbatim copies of `backend/layers/shared` and `backend/layers/jwt` with no bundling applied.
- The `dda-synthetic-data-handler` function must keep exactly three layers attached and its full configuration unchanged: runtime python3.11, handler `synthetic_data.handler`, 1024 MB memory, 15 min timeout, environment variables, IAM role and grants, and the `/synthetic/...` API route matrix.
- All 94 existing infrastructure tests must stay green.
- `backend/layers/imaging/build.sh` must continue to work for manual layer builds (it remains the documented standalone build path).

**Scope:**
All parts of the SyntheticDataStack other than the `SyntheticImagingLayer` asset production are out of scope and must be byte-equivalent in the synthesized template (modulo the imaging layer's asset hash). No other stack, handler code file, or layer source is touched.

## Hypothesized Root Cause

Confirmed by live evidence (no re-derivation needed):

1. **Convention without enforcement**: The imaging layer copied the jwt layer's "run `build.sh` before deploy" convention, but nothing in synth or deploy verifies the `python/` directory exists. `build.sh` was never run before the `cdk deploy` that created layer version 1.
2. **`python/` is not committed**: Unlike `layers/shared` (whose `python/` is tracked in git), the imaging layer's `python/` only exists after a manual build, so any fresh checkout deploys an empty layer.
3. **No synth-time or runtime guard**: `lambda.Code.fromAsset` on a plain directory stages whatever is there; an asset with zero Python content synthesizes and deploys cleanly.

## Correctness Properties

Property 1: Bug Condition - Synthesized Imaging Layer Asset Contains PIL

_For any_ synthesis of the SyntheticDataStack (isBugCondition on the unfixed code: the staged imaging layer asset lacks `python/PIL`), the fixed stack SHALL stage an imaging layer asset that contains `python/PIL` with the Pillow package installed from `requirements.txt`, so the deployed layer provides the PIL module to the handler.

**Validates: Requirements 2.1, 2.2**

Property 2: Preservation - Sibling Layers, Handler Wiring, and Manual Build Unchanged

_For any_ synthesis of the SyntheticDataStack where the bug condition does not apply (the shared/jwt layer staging, handler configuration, roles, and routes), the fixed stack SHALL produce the same result as the original stack: shared and jwt layer assets staged verbatim from their source directories, exactly three layers attached to the handler, unchanged function configuration, and all 94 existing infrastructure tests passing; `build.sh` SHALL continue to produce a `python/` directory with Pillow when run manually.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

## Fix Implementation

### Changes Required

**File**: `edge-cv-portal/infrastructure/lib/synthetic-data-stack.ts`

**Construct**: `SyntheticImagingLayer` (`lambda.LayerVersion` code asset)

**Specific Changes**:
1. **Bundled asset**: Replace the plain `lambda.Code.fromAsset(path.join(__dirname, '../../backend/layers/imaging'))` with a bundled asset:
   - `bundling.image: lambda.Runtime.PYTHON_3_11.bundlingImage`
   - `bundling.command: ['bash', '-c', 'pip install -r requirements.txt -t /asset-output/python']` (Docker path — inside the Python 3.11 Lambda bundling image, plain pip produces the correct linux/x86_64 wheels)
2. **Local bundling fallback** (`bundling.local.tryBundle`): run pip on the host with the same wheel targeting as `build.sh` (`--platform manylinux2014_x86_64 --implementation cp --python-version 3.11 --only-binary=:all:`) into `<outputDir>/python`. Return `true` on success so Docker is skipped; return `false` on any failure so CDK falls back to the Docker image. This keeps synth working on hosts without Docker (and in jest, where local bundling is far faster).
3. **Comment update**: Replace the "built by build.sh, same convention as the jwt layer" comment with the bundling explanation, noting `build.sh` remains for manual builds.
4. **Keep `build.sh`**: Left in place and unchanged — it is still a valid manual build path and documents the wheel-targeting flags the local bundling mirrors. (A pre-existing `python/` directory in the source dir is irrelevant post-fix: bundling output, not the raw directory, becomes the asset.)

No changes to `synthetic_data.py`, other stacks, or other layers.

## Testing Strategy

### Validation Approach

Two-phase: first a synth-level exploration test that demonstrates the empty asset on unfixed code, then fix verification plus preservation checks, followed by a live deploy of the synthetic-data stack and a runtime verification that the deployed handler imports PIL.

### Exploratory Bug Condition Checking

**Goal**: Surface the counterexample on UNFIXED code — the staged imaging layer asset has no `python/PIL` — confirming the (already live-confirmed) root cause at the synth level.

**Test Plan**: A jest test (`test/synthetic-imaging-layer-empty.test.ts`) synthesizes the SyntheticDataStack into a cloud assembly, resolves the `SyntheticImagingLayer` resource's staged asset path from the template's `aws:asset:path` metadata, and asserts the staged asset contains `python/PIL/` (and `requirements.txt`'s pinned Pillow content). Run on the UNFIXED stack to observe the failure.

**Test Cases**:
1. **Staged asset contains python/PIL**: Assert `<asset>/python/PIL` exists with Pillow module files (will fail on unfixed code — asset holds only `build.sh` + `requirements.txt`)

**Expected Counterexamples**:
- Staged imaging layer asset directory listing = `['build.sh', 'requirements.txt']`; no `python/` at all.

### Fix Checking

**Goal**: Verify that for all syntheses, the fixed stack stages a populated imaging layer asset.

**Pseudocode:**
```
FOR ALL synth WHERE isBugCondition would hold on the unfixed stack DO
  asset := stageImagingLayerAsset_fixed(synth)
  ASSERT exists(asset + "/python/PIL")
END FOR
```

The CDK synthesis is deterministic, so the exhaustive synth-level assertion (same test as exploration, now passing) is the all-inputs guarantee. Live fix checking on top: deploy the stack, confirm a new imaging layer version is attached to `dda-synthetic-data-handler`, inspect the deployed layer content for `python/PIL`, and exercise the PIL import path in the deployed function.

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed stack behaves identically to the original.

**Pseudocode:**
```
FOR ALL synth DO
  ASSERT sharedLayerAsset_fixed(synth) = verbatim copy of backend/layers/shared
  ASSERT jwtLayerAsset_fixed(synth)    = verbatim copy of backend/layers/jwt
  ASSERT handlerConfig_fixed(synth)    = handlerConfig_original(synth)  // 3 layers, runtime, memory, timeout, env
END FOR
```

**Testing Approach**: The synthesized template is deterministic, so exhaustive template/asset assertions provide the universal guarantee (same reasoning as the sibling `synthetic-data-s3-permissions` tests).

**Test Plan**: Observe on UNFIXED code that the shared/jwt assets are verbatim copies and the handler has three layers; encode those observations as assertions; verify they pass before AND after the fix.

**Test Cases**:
1. **Shared layer verbatim**: staged shared asset file set equals the `backend/layers/shared` source file set (including `python/shared_utils`)
2. **JWT layer verbatim**: staged jwt asset file set equals the `backend/layers/jwt` source file set (no bundling applied)
3. **Handler wiring**: exactly 3 layers on `dda-synthetic-data-handler`; runtime python3.11, handler `synthetic_data.handler`, MemorySize 1024, Timeout 900, environment variable set unchanged
4. **Full suite**: all 94 existing infrastructure tests stay green

### Unit Tests

- Synth-level asset content assertions (exploration/fix test above)
- Handler configuration and layer-count template assertions (preservation)

### Property-Based Tests

- The synthesized template and staged assets are deterministic functions of the source tree; the exhaustive template/asset assertions quantify over every layer asset and every handler property, serving as the property check (consistent with prior infra bugfix specs in this repo)

### Integration Tests

- `cdk deploy` of the synthetic-data stack (account 164152369890, us-east-1), honoring `.kiro/steering/builds.md` (no concurrent component builds; move `cdk.out` aside per steering)
- Live verification: new imaging layer version attached to `dda-synthetic-data-handler`; deployed layer zip contains `python/PIL`; a direct Lambda invoke exercising the PIL import path succeeds (real user claims: user_id `a4b804e8-5061-7004-12f2-38a0149dcd4c`, usecase `645504ce-a60a-4009-8349-7548c0025cd3`, bucket `ryvan-cookies`)
