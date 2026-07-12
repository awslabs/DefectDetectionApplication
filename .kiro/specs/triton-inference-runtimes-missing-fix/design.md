# Triton Inference Runtimes Missing Bugfix Design

## Overview

On-device Triton models deployed through Greengrass LocalServer components get stuck in a "loading" state and never transition to `AVAILABLE`. The Triton Python backend fails with `ModuleNotFoundError: No module named 'inference_runtimes'`, and `base_*` models report `UNAVAILABLE`.

The defect lives in `cp_model_conversion_files()` in `src/backend/dda_triton/triton_setup.py`. On first-time setup the function copies the entire `resources_for_copy` tree (including `inference_runtimes.py`) with `shutil.copytree`. On every subsequent setup — when `/aws_dda/resources_for_copy` already exists — it instead re-copies only a hand-maintained allowlist, `files_to_copy_resources = ["ensemble_model", "lfv_model_template.py", "marshal_for_capture_template.py"]`, which omits `inference_runtimes.py`. Devices provisioned before `inference_runtimes.py` existed therefore receive the updated `lfv_model_template.py` (which does `import inference_runtimes`) but never receive `inference_runtimes.py`. Downstream, `model_convertor.py` only stages the module into each model version directory when it is present in `/aws_dda/resources_for_copy`; because it is absent it logs a warning and the module never lands next to `lfv_model_template.py`, so the Triton Python backend import fails.

The fix ensures `inference_runtimes.py` reaches already-provisioned devices on the subsequent-setup path so that `model_convertor.py` can stage it and models become `AVAILABLE`. The strategy is a targeted change to the subsequent-setup copy path in `cp_model_conversion_files()`, deployed by rebuilding and republishing the LocalServer components.

## Glossary

- **Bug_Condition (C)**: The condition that triggers the bug — `cp_model_conversion_files()` runs on the subsequent-setup path (`/aws_dda/resources_for_copy` already exists) and the copy set does not include `inference_runtimes.py`.
- **Property (P)**: The desired behavior — after the fixed function runs on the subsequent-setup path, `inference_runtimes.py` is present in `/aws_dda/resources_for_copy` so it can be staged downstream.
- **Preservation**: Existing behavior that must remain unchanged — first-time `copytree` setup, the other allowlisted resources, the `files_to_copy_to_dda_triton`/`files_to_copy_to_aws_dda` copies, the DLR-only path in `model_convertor.py`, and already-healthy devices.
- **cp_model_conversion_files**: The function in `src/backend/dda_triton/triton_setup.py` that copies model-conversion resource files into `/dda_triton`, `/aws_dda`, and `/aws_dda/resources_for_copy`.
- **files_to_copy_resources**: The hand-maintained allowlist used on the subsequent-setup path to re-copy individual resource entries into `/aws_dda/resources_for_copy`.
- **resources_for_copy**: The source resource directory (`/dda_triton/resources_for_copy/`) whose contents must reach `/aws_dda/resources_for_copy` on every device.
- **model_convertor.py**: The module that stages `resources_for_copy` files (including `inference_runtimes.py`) into each model's version directory next to `lfv_model_template.py`.
- **Subsequent-setup path**: The `else` branch taken when `/aws_dda/resources_for_copy` already exists.

## Bug Details

### Bug Condition

The bug manifests when `cp_model_conversion_files()` runs on a device where `/aws_dda/resources_for_copy` already exists (the subsequent-setup path). In that branch the function re-copies only the entries in `files_to_copy_resources`, and because `inference_runtimes.py` is not in that allowlist, the file is never delivered to already-provisioned devices — even though `lfv_model_template.py` (which is delivered) imports it.

**Formal Specification:**
```
FUNCTION isBugCondition(input)
  INPUT: input of type SetupInvocation
  OUTPUT: boolean

  RETURN input.resourcesDirExists = TRUE
         AND "inference_runtimes.py" NOT IN input.files_to_copy_resources
END FUNCTION
```

### Examples

- **Already-provisioned JP6 device (bug)**: `/aws_dda/resources_for_copy` exists → subsequent-setup path runs → `inference_runtimes.py` is not copied → `model_convertor.py` logs "not found" and skips staging → model load raises `ModuleNotFoundError: No module named 'inference_runtimes'` → model stuck loading, `base_*` reports `UNAVAILABLE`. Expected: `inference_runtimes.py` present and model `AVAILABLE`.
- **Already-provisioned segmentation model (bug)**: Same path; segmentation model also fails to load for the identical reason. Expected: model loads and reports `AVAILABLE`.
- **First-time setup (no bug)**: `/aws_dda/resources_for_copy` does not exist → `copytree` copies the whole tree including `inference_runtimes.py` → model loads correctly. Expected and actual match.
- **DLR-only device edge case (no bug)**: `inference_runtimes.py` legitimately absent; `model_convertor.py` proceeds with only a warning and the DLR path loads normally. Expected behavior unchanged.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**
- First-time setup (when `/aws_dda/resources_for_copy` does not exist) SHALL continue to copy the entire `resources_for_copy` tree via `copytree` (Requirement 3.1).
- `files_to_copy_to_dda_triton` and `files_to_copy_to_aws_dda` copies SHALL continue exactly as before, along with the existing `files_to_copy_resources` entries (`ensemble_model`, `lfv_model_template.py`, `marshal_for_capture_template.py`) (Requirement 3.2).
- On DLR-only devices where `inference_runtimes.py` is legitimately absent, `model_convertor.py` SHALL continue without error (Requirement 3.3).
- Devices that already had `inference_runtimes.py` staged SHALL continue to load models and report `AVAILABLE` (Requirement 3.4).

**Scope:**
All inputs that do NOT involve the subsequent-setup path missing `inference_runtimes.py` should be completely unaffected by this fix. This includes:
- First-time setup invocations (the `copytree` branch)
- The `/dda_triton` and `/aws_dda` file copies
- The DLR-only staging path in `model_convertor.py`

**Note:** The expected correct behavior for buggy inputs is defined in the Correctness Properties section (Property 1).

## Hypothesized Root Cause

The root cause is confirmed. The analysis below records why the confirmed cause is correct and the contributing factors.

1. **Hand-maintained allowlist drift (confirmed primary cause)**: The subsequent-setup path copies only `files_to_copy_resources`, which was never updated when `inference_runtimes.py` was added to `resources_for_copy`. New resource files silently fail to reach already-provisioned devices.
   - First-time setup uses `copytree` (whole tree) so newly provisioned devices are unaffected, which masked the defect.
   - Only devices provisioned before `inference_runtimes.py` existed hit the missing-file path.

2. **Silent downstream skip**: `model_convertor.py` treats a missing `inference_runtimes.py` as a warning (to support DLR-only devices), so the missing file does not fail setup loudly — it surfaces only later as a Triton import error.

3. **Template/runtime coupling**: `lfv_model_template.py` unconditionally imports `inference_runtimes`, so once the updated template ships, any device missing the runtime module fails to load models.

4. **Not a `sys.path` problem**: The `sys.path.insert` guards already added to `lfv_model_template.py` correctly make a *present* sibling module importable, but they cannot resolve a file that was never delivered. That change is complementary, not the primary fix.

## Correctness Properties

Property 1: Bug Condition - inference_runtimes.py delivered on subsequent setup

_For any_ setup invocation where the bug condition holds (`isBugCondition` returns true — the resources directory already exists and the copy set omits `inference_runtimes.py`), the fixed `cp_model_conversion_files` function SHALL result in `inference_runtimes.py` being present in `/aws_dda/resources_for_copy` so that `model_convertor.py` can stage it into each model version directory and models transition to `AVAILABLE`.

**Validates: Requirements 2.1, 2.2, 2.3**

Property 2: Preservation - Non-buggy invocations unchanged

_For any_ setup invocation where the bug condition does NOT hold (`isBugCondition` returns false — first-time setup via `copytree`, or a device/path not on the missing-file subsequent-setup branch), the fixed function SHALL produce the same result as the original function, preserving first-time `copytree` behavior, the `/dda_triton` and `/aws_dda` file copies, the other allowlisted resources, and the unchanged DLR-only staging path.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4**

## Fix Implementation

### Changes Required

Assuming our (confirmed) root cause analysis is correct:

**File**: `src/backend/dda_triton/triton_setup.py`

**Function**: `cp_model_conversion_files()`

**Recommended approach — re-sync the whole resource directory on the subsequent-setup path (drift-proof):**

Rather than only patching the allowlist, make the subsequent-setup branch copy the full contents of the source `resources_for_copy` directory so that any resource file (present or future) is always delivered. This eliminates the recurring maintenance hazard that caused this bug.

1. **Replace the allowlist loop with a full directory re-sync**: In the `else` branch (where `/aws_dda/resources_for_copy` already exists), copy the entire source `resources_for_copy/` contents into the destination instead of iterating a fixed list. Use `shutil.copytree(src, dst, dirs_exist_ok=True)` (Python 3.8+) so existing subtrees such as `ensemble_model` are merged/updated in place and new files like `inference_runtimes.py` are added.
2. **Verify Python version support**: Confirm the on-device Python runtime is 3.8+ so `dirs_exist_ok=True` is available; the LocalServer components ship a modern Python, so this holds. If a lower version must be supported, fall back to iterating `os.listdir(source resources dir)` and copying each entry (`shutil.copy2` for files, `shutil.copytree(..., dirs_exist_ok=True)` for directories).
3. **Preserve first-time path**: Leave the `if not os.path.exists(...)` first-time `copytree` branch untouched (Requirement 3.1).
4. **Preserve other copies**: Leave `files_to_copy_to_dda_triton` and `files_to_copy_to_aws_dda` handling untouched (Requirement 3.2).
5. **Keep the complementary `sys.path` guard**: The `sys.path.insert` in `lfv_model_template.py` remains; it ensures the now-delivered sibling module is importable regardless of the Triton stub's CWD.

**Tradeoff discussion:**

- *Minimal fix (add `"inference_runtimes.py"` to `files_to_copy_resources`)*: Smallest possible change, lowest behavioral risk, trivially satisfies Property 1 for this file. Downside: the allowlist remains hand-maintained, so the next new resource file will reintroduce the same class of bug (drift).
- *Full re-sync (recommended)*: Drift-proof — every resource file is delivered automatically, so this bug cannot recur when new resources are added. Downside: it is a behavioral change on the subsequent-setup path — it now overwrites/updates all resource files (not just three), so any on-device manual edits to those files would be replaced, and directory merge semantics (`dirs_exist_ok=True`) apply to subtrees like `ensemble_model`.

**Recommendation**: Adopt the full re-sync approach for drift-proofing. The overwrite behavior is acceptable and desirable here because these files are canonical app-shipped resources that should always match the deployed component; on-device manual edits are not a supported workflow. The regression-prevention constraints (3.1–3.4) remain intact: the first-time path is unchanged, the other resources are still copied (now guaranteed), the additional `/dda_triton` and `/aws_dda` copies are unchanged, and the DLR-only path in `model_convertor.py` is untouched. If the team prefers the absolute lowest-risk change for this release, the minimal allowlist addition is an acceptable fallback that still satisfies Property 1.

### Deployment Path

The fix ships by rebuilding and republishing the LocalServer components:
1. Rebuild/republish the JP6 LocalServer component first (`LocalServer.arm64JP6`), since JP6 devices are the confirmed affected fleet.
2. Then rebuild/republish the JP5 LocalServer component (`LocalServer.arm64JP5`).
3. Redeploy to affected devices; on the next component setup, `cp_model_conversion_files()` runs and delivers `inference_runtimes.py`.

## Testing Strategy

### Validation Approach

The testing strategy follows a two-phase approach: first, surface counterexamples that demonstrate the bug on the unfixed code, then verify the fix delivers `inference_runtimes.py` and preserves all existing behavior.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE implementing the fix, and confirm the root cause. If refuted, re-hypothesize.

**Test Plan**: In a temp-directory harness, pre-create the destination `resources_for_copy` (to force the subsequent-setup path) with a stale set of files that omits `inference_runtimes.py`, populate a source `resources_for_copy` that includes `inference_runtimes.py`, run `cp_model_conversion_files()`, and assert on whether `inference_runtimes.py` lands in the destination. Run on the UNFIXED code to observe the failure.

**Test Cases**:
1. **Subsequent-setup missing-file test**: Destination exists without `inference_runtimes.py`; after running setup, assert the file is present in the destination (will fail on unfixed code).
2. **Downstream staging test**: With `inference_runtimes.py` absent from `/aws_dda/resources_for_copy`, run `model_convertor.py` staging and assert the file is NOT in the model version dir and only a warning is logged (reproduces the skip on unfixed code).
3. **Import-failure reproduction**: Load a model whose version dir lacks `inference_runtimes.py` and assert `ModuleNotFoundError: No module named 'inference_runtimes'` (will fail/raise on unfixed code).
4. **Edge case — first-time setup**: Destination does not exist; assert `copytree` delivers `inference_runtimes.py` (passes on unfixed code, confirming the bug is specific to the subsequent-setup path).

**Expected Counterexamples**:
- After subsequent-setup on unfixed code, `/aws_dda/resources_for_copy/inference_runtimes.py` does not exist.
- Possible causes (confirmed): allowlist omits the file; downstream staging skips absent file; template import then fails.

### Fix Checking

**Goal**: Verify that for all inputs where the bug condition holds, the fixed function produces the expected behavior (the file is delivered).

**Pseudocode:**
```
FOR ALL input WHERE isBugCondition(input) DO
  cp_model_conversion_files_fixed(input)
  ASSERT fileExists("/aws_dda/resources_for_copy/inference_runtimes.py")
END FOR
```

### Preservation Checking

**Goal**: Verify that for all inputs where the bug condition does NOT hold, the fixed function produces the same result as the original function.

**Pseudocode:**
```
FOR ALL input WHERE NOT isBugCondition(input) DO
  ASSERT cp_model_conversion_files_original(input) = cp_model_conversion_files_fixed(input)
END FOR
```

**Testing Approach**: Property-based testing is recommended for preservation checking because:
- It generates many source/destination directory layouts automatically across the input domain.
- It catches edge cases (empty dirs, extra files, nested subtrees) that manual unit tests might miss.
- It provides strong guarantees that behavior is unchanged for all non-buggy inputs.

**Test Plan**: Observe behavior on UNFIXED code first for first-time setup and DLR-only staging, then write property-based tests that assert the fixed function's resulting destination file set equals the original's for those non-buggy inputs.

**Test Cases**:
1. **First-time setup preservation**: Observe `copytree` delivers the full tree on unfixed code; assert the fixed function's destination contents are identical.
2. **Auxiliary copies preservation**: Assert `files_to_copy_to_dda_triton` and `files_to_copy_to_aws_dda` destinations are identical before and after the fix.
3. **DLR-only staging preservation**: Observe `model_convertor.py` proceeds with a warning when `inference_runtimes.py` is absent on unfixed code; assert this is unchanged after the fix.

### Unit Tests

- Subsequent-setup path delivers `inference_runtimes.py` into the destination.
- Subsequent-setup path still delivers `ensemble_model`, `lfv_model_template.py`, `marshal_for_capture_template.py`.
- First-time path still performs the full `copytree`.
- `model_convertor.py` stages `inference_runtimes.py` into the model version dir when present, and warns (no error) when absent.

### Property-Based Tests

- Generate random destination directory states (present/absent files, extra files, nested subtrees) with the destination existing → assert `inference_runtimes.py` is always present after the fixed function runs (Property 1 / Fix Checking).
- Generate random source/destination layouts for non-buggy inputs (first-time setup; DLR-only staging) → assert fixed and original produce identical destination file sets (Property 2 / Preservation Checking).
- Generate varied resource file sets in the source → assert the full re-sync delivers every source file to the destination (drift-proofing).

### Integration Tests

- Full on-device (or emulated) flow: run setup on an already-provisioned device state, then load a detection model and a segmentation model; assert both transition to `AVAILABLE` and `base_*` no longer reports `UNAVAILABLE`.
- Context/version verification: after setup, assert the model version directory contains `inference_runtimes.py` next to `lfv_model_template.py`.
- Deployment verification (JP6 then JP5): after rebuilding/republishing the LocalServer component and redeploying, confirm on-device that (a) `/aws_dda/resources_for_copy/inference_runtimes.py` exists, (b) each model version dir contains `inference_runtimes.py`, and (c) the model transitions to `AVAILABLE` without the `ModuleNotFoundError`.
