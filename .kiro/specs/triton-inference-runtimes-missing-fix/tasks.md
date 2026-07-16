# Implementation Plan

## Overview

This plan fixes the missing `inference_runtimes.py` on the subsequent-setup path in `cp_model_conversion_files()` (`src/backend/dda_triton/triton_setup.py`) using the exploratory bugfix workflow: reproduce the bug first (Property 1: Bug Condition), capture existing behavior (Property 2: Preservation), apply the drift-proof full re-sync fix, then validate and deploy (JP6 then JP5).

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2"],
      "description": "Write tests against UNFIXED code: task 1 (Bug Condition) fails, task 2 (Preservation) passes. Independent of each other."
    },
    {
      "wave": 2,
      "tasks": ["3"],
      "description": "Implement the drift-proof re-sync fix (3.1), then re-run task 1 (3.2) and task 2 (3.3). Depends on wave 1."
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "Add drift-proofing and downstream staging tests. Depends on wave 2."
    },
    {
      "wave": 4,
      "tasks": ["5"],
      "description": "Deploy (JP6 then JP5) and verify on-device. Depends on wave 3."
    },
    {
      "wave": 5,
      "tasks": ["6"],
      "description": "Checkpoint - ensure all tests pass. Depends on wave 4."
    }
  ]
}
```

- Tasks 1 and 2 are independent and must be completed BEFORE task 3 (tests written against unfixed code).
- Task 3 depends on 1 and 2. Sub-tasks 3.2 and 3.3 depend on 3.1.
- Task 4 depends on 3. Task 5 depends on 4. Task 6 depends on 5.

## Tasks

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - inference_runtimes.py missing on subsequent setup
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bug exists
  - **Scoped PBT Approach**: The bug is deterministic on the subsequent-setup path, so scope the property to the concrete failing configuration — the destination `/aws_dda/resources_for_copy` already exists (subsequent-setup path) and the stale destination file set omits `inference_runtimes.py`, while the source `resources_for_copy` includes it. Vary the incidental destination contents (extra/stale files, nested subtrees) to show the counterexample holds across layouts.
  - Build a temp-directory harness that mirrors the layout `cp_model_conversion_files()` expects: a source `resources_for_copy/` containing `inference_runtimes.py`, `ensemble_model`, `lfv_model_template.py`, `marshal_for_capture_template.py`, and a pre-created destination `resources_for_copy` that omits `inference_runtimes.py` (forcing the `else` subsequent-setup branch). Patch/redirect `DDA_ROOT_FOLDER`, `DDA_TRITON_FOLDER`, source folder, and the `/aws_dda/resources_for_copy` path so the test does not touch real system directories (from Bug Condition `isBugCondition(input)` in design)
  - Run `cp_model_conversion_files()` (from `src/backend/dda_triton/triton_setup.py`) and assert `inference_runtimes.py` is present in the destination `resources_for_copy` — the assertion encodes Expected Behavior 2.1
  - Add a downstream staging counterexample: with `inference_runtimes.py` absent from the destination `resources_for_copy`, run the `model_convertor.py` staging path (Line ~392-409) and assert the file is NOT staged into the model version dir and only a warning is logged (reproduces Bug 1.2)
  - Add an import-failure reproduction: a model version dir lacking `inference_runtimes.py` where `lfv_model_template.py` imports it raises `ModuleNotFoundError: No module named 'inference_runtimes'` (reproduces Bug 1.3)
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct - it proves the bug exists)
  - Document counterexamples found (e.g., "after subsequent-setup on unfixed code, `<dest>/resources_for_copy/inference_runtimes.py` does not exist")
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 2.1_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Non-buggy invocations unchanged
  - **IMPORTANT**: Follow observation-first methodology - run the UNFIXED code first, record actual outputs, then assert those outputs
  - Observe on UNFIXED code: first-time setup (destination `resources_for_copy` does NOT exist) → `copytree` delivers the entire source tree (`inference_runtimes.py`, `ensemble_model`, `lfv_model_template.py`, `marshal_for_capture_template.py`). Record the resulting destination file set
  - Observe on UNFIXED code: `files_to_copy_to_dda_triton` (`constants.py`, `model_config_pb2.py`, `model_autostart_utils.py`) land in `/dda_triton`, and `files_to_copy_to_aws_dda` (`model_convertor.py`, `convert_model_cleanup.py`, `model_conversion_requirements.txt`) land in `/aws_dda`. Record these destinations
  - Observe on UNFIXED code: DLR-only staging in `model_convertor.py` where `inference_runtimes.py` is legitimately absent → the function proceeds with only a warning (no error, no crash). Record this behavior
  - Write property-based tests (generate random destination directory states for the first-time path: absent/extra files, nested subtrees) asserting the fixed function's resulting destination contents equal the original's for these non-buggy inputs (from Preservation Requirements in design)
  - Write a property-based test asserting the `/dda_triton` and `/aws_dda` auxiliary copies are identical before and after the fix
  - Write a preservation test asserting DLR-only staging still proceeds with a warning and no error
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 3. Fix for inference_runtimes.py missing on subsequent-setup path

  - [x] 3.1 Implement the drift-proof re-sync in cp_model_conversion_files()
    - In `src/backend/dda_triton/triton_setup.py`, replace the `else` branch allowlist loop (which iterates `files_to_copy_resources` and copies only `ensemble_model`, `lfv_model_template.py`, `marshal_for_capture_template.py`) with a full re-sync of the source `resources_for_copy` directory
    - Use `shutil.copytree(source_folder + "resources_for_copy/", "/aws_dda/resources_for_copy", dirs_exist_ok=True)` so `ensemble_model` and other subtrees merge/update in place and `inference_runtimes.py` (and any future resource file) is always delivered
    - Confirm the on-device Python runtime is 3.8+ (required for `dirs_exist_ok`); if a lower version must be supported, fall back to iterating `os.listdir` of the source resources dir (`shutil.copy2` for files, `shutil.copytree(..., dirs_exist_ok=True)` for directories)
    - Leave the first-time `if not os.path.exists("/aws_dda/resources_for_copy/")` `copytree` branch untouched
    - Leave `files_to_copy_to_dda_triton` and `files_to_copy_to_aws_dda` handling untouched
    - Leave the `model_convertor.py` DLR-only staging path (Line ~392-409) untouched — it already stages `inference_runtimes.py` when present and warns when absent
    - _Bug_Condition: isBugCondition(input) where input.resourcesDirExists = TRUE AND "inference_runtimes.py" NOT IN input.files_to_copy_resources_
    - _Expected_Behavior: after cp_model_conversion_files'(input), fileExists("/aws_dda/resources_for_copy/inference_runtimes.py")_
    - _Preservation: first-time copytree, /dda_triton and /aws_dda copies, other allowlisted resources, DLR-only staging path all unchanged_
    - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.4_

  - [x] 3.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - inference_runtimes.py delivered on subsequent setup
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - The test from task 1 encodes the expected behavior; when it passes it confirms `inference_runtimes.py` is delivered to `/aws_dda/resources_for_copy` on the subsequent-setup path
    - Run the bug condition exploration test from task 1
    - **EXPECTED OUTCOME**: Test PASSES (confirms bug is fixed)
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 3.3 Verify preservation tests still pass
    - **Property 2: Preservation** - Non-buggy invocations unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run the preservation property tests from task 2
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions on first-time setup, auxiliary copies, and DLR-only staging)
    - Confirm all tests still pass after the fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [ ] 4. Add drift-proofing and downstream staging tests
  - **Property 1: Fix Checking (PBT)** - Generate varied source resource file sets (add arbitrary new resource files alongside `inference_runtimes.py`) and varied pre-existing destination states → assert the fixed re-sync delivers every source file to `/aws_dda/resources_for_copy`, proving the drift class of bug cannot recur
  - Verify downstream staging: with `inference_runtimes.py` now present in `/aws_dda/resources_for_copy`, run the `model_convertor.py` staging path and assert `inference_runtimes.py` is copied next to `lfv_model_template.py` into the model version directory (Requirement 2.2)
  - Run all tests
  - **EXPECTED OUTCOME**: All tests PASS
  - _Requirements: 2.1, 2.2, 3.2_

- [ ] 5. Deploy and verify on-device (JP6 first, then JP5)
  - Rebuild and republish the JP6 LocalServer component (`LocalServer.arm64JP6`) first, since JP6 devices are the confirmed affected fleet
  - Rebuild and republish the JP5 LocalServer component (`LocalServer.arm64JP5`)
  - **NOTE**: These are build/publish/deploy steps that must be run manually — provide the exact commands to the user rather than running long-running processes here
  - Redeploy to affected devices so `cp_model_conversion_files()` runs on the next component setup
  - Confirm on-device: `/aws_dda/resources_for_copy/inference_runtimes.py` exists
  - Confirm on-device: each model version directory contains `inference_runtimes.py` next to `lfv_model_template.py`
  - Confirm on-device: detection and segmentation models transition to `AVAILABLE`, `base_*` no longer reports `UNAVAILABLE`, and no `ModuleNotFoundError: No module named 'inference_runtimes'` appears in the Triton logs
  - _Requirements: 2.3, 3.4_

- [ ] 6. Checkpoint - Ensure all tests pass
  - Ensure all unit, property-based, and staging tests pass; ask the user if questions arise.

## Notes

- **Test-first ordering is mandatory**: Task 1 (bug condition) must FAIL and task 2 (preservation) must PASS on the UNFIXED code before implementing task 3. Do not modify `triton_setup.py` until both are written and their expected outcomes documented.
- **Property references**: Property 1 (Fix Checking) validates Requirements 2.1, 2.2, 2.3; Property 2 (Preservation) validates Requirements 3.1, 3.2, 3.3, 3.4.
- **Primary fix location**: the `else` (subsequent-setup) branch of `cp_model_conversion_files()` in `src/backend/dda_triton/triton_setup.py`. Do not touch the first-time `copytree` branch, the `/dda_triton` and `/aws_dda` copies, or the `model_convertor.py` DLR-only staging path.
- **Deployment (task 5) is manual**: rebuild/republish and redeploy commands are long-running and must be run by the user in their terminal, not by the agent.
