# Bugfix Requirements Document

## Introduction

On-device Triton models deployed via Greengrass LocalServer components (e.g. `jp6-orinagx` running `LocalServer.arm64JP6`) get stuck in a "loading" state and never transition to `AVAILABLE`. The Triton server logs report `ModuleNotFoundError: No module named 'inference_runtimes'`, and the `base_*` models report `UNAVAILABLE`. As a result, users cannot deploy or run models (both detection and segmentation) on affected devices.

The root cause is in `cp_model_conversion_files()` in `src/backend/dda_triton/triton_setup.py`. On a first-time setup the function copies the entire `resources_for_copy` tree (including `inference_runtimes.py`) via `shutil.copytree`. On subsequent setups, when `/aws_dda/resources_for_copy` already exists, it only re-copies files in the hand-maintained `files_to_copy_resources` allowlist, which omits `inference_runtimes.py`. Devices provisioned before this file existed therefore receive the updated `lfv_model_template.py` (which imports `inference_runtimes`) but never receive `inference_runtimes.py` itself. Downstream, `model_convertor.py` only stages the module into each model's version directory when it is present in `/aws_dda/resources_for_copy`; because it is absent, the Triton Python backend import fails and models remain stuck loading.

This bugfix ensures the required runtime resource file is delivered to already-provisioned devices so model conversion can stage it correctly and models become `AVAILABLE`.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN `cp_model_conversion_files()` runs on a device where `/aws_dda/resources_for_copy` already exists THEN the system copies only the files listed in `files_to_copy_resources` and does not copy `inference_runtimes.py` into `/aws_dda/resources_for_copy`

1.2 WHEN `model_convertor.py` stages resource files into a model's version directory AND `inference_runtimes.py` is absent from `/aws_dda/resources_for_copy` THEN the system skips copying it and logs only a warning, leaving the module unstaged next to `lfv_model_template.py`

1.3 WHEN the Triton Python backend loads a model whose version directory is missing `inference_runtimes.py` THEN the system raises `ModuleNotFoundError: No module named 'inference_runtimes'`, the model stays stuck in "loading" and the `base_*` models report `UNAVAILABLE`

### Expected Behavior (Correct)

2.1 WHEN `cp_model_conversion_files()` runs on a device where `/aws_dda/resources_for_copy` already exists THEN the system SHALL copy `inference_runtimes.py` into `/aws_dda/resources_for_copy` so it is present for downstream staging

2.2 WHEN `model_convertor.py` stages resource files into a model's version directory AND `inference_runtimes.py` is present in `/aws_dda/resources_for_copy` THEN the system SHALL copy it next to `lfv_model_template.py` in the model version directory

2.3 WHEN the Triton Python backend loads a model on an already-provisioned device after the fix is deployed THEN the system SHALL import `inference_runtimes` successfully and the model SHALL transition to `AVAILABLE` without getting stuck loading

### Unchanged Behavior (Regression Prevention)

3.1 WHEN `cp_model_conversion_files()` runs on a device where `/aws_dda/resources_for_copy` does NOT yet exist THEN the system SHALL CONTINUE TO copy the entire `resources_for_copy` tree (including `inference_runtimes.py`, `ensemble_model`, `lfv_model_template.py`, and `marshal_for_capture_template.py`) via `copytree`

3.2 WHEN `cp_model_conversion_files()` runs THEN the system SHALL CONTINUE TO copy the existing `files_to_copy_to_dda_triton` and `files_to_copy_to_aws_dda` files and the other entries already in `files_to_copy_resources` (`ensemble_model`, `lfv_model_template.py`, `marshal_for_capture_template.py`)

3.3 WHEN `model_convertor.py` stages resources on a DLR-only device where `inference_runtimes.py` is legitimately absent THEN the system SHALL CONTINUE TO proceed without error (the DLR path remains unchanged)

3.4 WHEN a model is loaded on a device that was already functioning correctly (already had `inference_runtimes.py` staged) THEN the system SHALL CONTINUE TO load the model and report it as `AVAILABLE`

## Bug Condition and Property Specification

### Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type SetupInvocation
  OUTPUT: boolean

  // The bug triggers on the "subsequent setup" path: the resources
  // destination already exists, so only the allowlist is re-copied, and the
  // allowlist does not include inference_runtimes.py.
  RETURN X.resourcesDirExists = TRUE
     AND "inference_runtimes.py" NOT IN X.files_to_copy_resources
END FUNCTION
```

### Property: Fix Checking

```pascal
// For every subsequent-setup invocation, inference_runtimes.py must end up
// present in the resources_for_copy destination so it can be staged downstream.
FOR ALL X WHERE isBugCondition(X) DO
  cp_model_conversion_files'(X)
  ASSERT fileExists("/aws_dda/resources_for_copy/inference_runtimes.py")
END FOR
```

### Property: Preservation Checking

```pascal
// For all non-buggy inputs (e.g. first-time setup where the directory does not
// yet exist, or DLR-only devices), the fixed function behaves identically to
// the original.
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT cp_model_conversion_files(X) = cp_model_conversion_files'(X)
END FOR
```

**Key Definitions:**
- **F**: `cp_model_conversion_files()` as it exists before the fix
- **F'**: `cp_model_conversion_files()` after adding `inference_runtimes.py` to the subsequent-setup copy path
