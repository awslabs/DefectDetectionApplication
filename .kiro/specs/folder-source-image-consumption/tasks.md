# Implementation Plan

## Overview

This plan fixes the missing folder-source image consumption in the workflow engine executor using the exploratory bugfix workflow: reproduce the bug first (Property 1: Bug Condition), capture existing behavior (Property 2: Preservation), apply the fix (record resolved Folder_Frames in `_stage_frame_sources`, consume on pipeline success, relocate to `failed/` on failure — mirroring the legacy `_cleanup_file_after_processing` / `_move_bad_folder_image_source` semantics), then validate and verify on hardware.

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
      "description": "Implement the fix (3.1), then re-run task 1 (3.2) and task 2 (3.3). Depends on wave 1."
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "Checkpoint - run the backend suites, no new failures. Depends on wave 2."
    },
    {
      "wave": 4,
      "tasks": ["5"],
      "description": "On-hardware verification on the JP6 (user-gated, requires the NEXT LocalServer build). Depends on wave 3."
    }
  ]
}
```

- Tasks 1 and 2 are independent and must be completed BEFORE task 3 (tests written against unfixed code).
- Task 3 depends on 1 and 2. Sub-tasks 3.2 and 3.3 depend on 3.1.
- Task 4 depends on 3. Task 5 depends on 4 and is user-gated.

## Tasks

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Folder Source Frames Are Consumed After the Run
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface counterexamples that demonstrate the missing consumption path
  - **Scoped PBT Approach**: The bug is deterministic - scope the property to concrete folder states (2-3 fake JPEGs at distinct mtimes in `tmp_path`) with a Hypothesis-generated file-count/mtime spread where useful
  - Create `test/backend-test/workflow_engine/test_folder_source_consumption_exploration.py` following the harness patterns in `test/backend-test/workflow_engine/workflow_engine_test_utils.py` and `test/backend-test/output_bindings_fixes/executor_harness.py` (stubbed `pipeline_manager_factory`, in-memory session, `tmp_path` folders)
  - Test cases (from Bug Condition in design): folder drains on successful run (next run resolves next-oldest); staged `.dda_decoded.png` deleted on JP6 `pngdec` chain; consumption still happens when the post-run handler raises `OutputBindingError`; corrupt image relocated to `{INFERENCE_RESULTS_DIR}/{workflow_id}/failed/`; pipeline failure relocates the resolved JPEG to `failed/`
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (this is correct - it proves the bug exists)
  - Document counterexamples found (e.g. "same oldest JPEG re-resolved on run 2; staged PNG left in folder")
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Non-Folder Runs and Run Semantics Unchanged
  - **IMPORTANT**: Follow observation-first methodology - observe UNFIXED behavior first, then encode it
  - Create `test/backend-test/workflow_engine/test_folder_source_consumption_preservation.py`
  - Observe on UNFIXED code: single-file `filesrc` runs leave the file in place and work repeatedly; camera/no-filesrc documents touch no files; `_stage_frame_sources` resolves the oldest JPEG by mtime; existing failure paths record the same status/error/`failing_node_id`
  - Write property-based tests (Hypothesis, per repo convention): for any single-file location the file is never deleted/relocated; for any folder of JPEGs with random mtimes the oldest is resolved; for non-filesrc documents the execution outcome and filesystem are untouched
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 3. Fix folder-source image consumption in the workflow engine executor

  - [x] 3.1 Implement the fix in `src/backend/workflow_engine/pipeline_executor.py`
    - Add a `FolderFrame` dataclass `(original, staged_png, node_id)` and change `_stage_frame_sources` to return `List[FolderFrame]`, recording an entry ONLY when the location was a directory; attach the resolved source path to `FrameSourceError` when JP6 staging fails for a directory-resolved JPEG
    - Add `_consume_folder_frames(folder_frames)`: delete staged PNG (if any) and original JPEG via `captured_images_utils.delete_image` (lazy import, same permission handling as legacy `_cleanup_file_after_processing`); best-effort per file, log-and-continue
    - Add `_relocate_failed_folder_frames(workflow_id, folder_frames)`: mirror `_move_bad_folder_image_source` - create `{constants.INFERENCE_RESULTS_DIR}/{workflow_id}/failed/` via `dda_user_management_utils.create_dda_user_directory` (lazy import) and `os.rename` originals into it; best-effort remove staged PNGs
    - Hook `execute()`: capture the returned `folder_frames`; relocate the bad image in the `except FrameSourceError` handler at the staging call site; relocate all recorded frames in the pipeline-run `except` handler (before existing failure handling, which is unchanged); call `_consume_folder_frames` immediately after the pipeline run returns successfully (before `_repair_capture_artifacts`), so consumption happens regardless of Bedrock/LLM/output-binding outcomes - mirroring the legacy `else` branch
    - Every new path is a no-op when `folder_frames` is empty, so non-folder runs take the exact pre-fix path
    - _Bug_Condition: isBugCondition(run) - a filesrc element with a directory location, from design_
    - _Expected_Behavior: Property 1 - consume on pipeline success, relocate on failure, from design_
    - _Preservation: Preservation Requirements from design - single-file/camera sources, selection order, run semantics unchanged_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 3.1, 3.2, 3.3, 3.4_

  - [x] 3.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Folder Source Frames Are Consumed After the Run
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - The test from task 1 encodes the expected behavior; when it passes, the expected behavior is satisfied
    - Run `python -m pytest test/backend-test/workflow_engine/test_folder_source_consumption_exploration.py`
    - **EXPECTED OUTCOME**: Test PASSES (confirms the bug is fixed)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5_

  - [x] 3.3 Verify preservation tests still pass
    - **Property 2: Preservation** - Non-Folder Runs and Run Semantics Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run `python -m pytest test/backend-test/workflow_engine/test_folder_source_consumption_preservation.py`
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the backend test suite (at minimum `test/backend-test/workflow_engine/` and `test/backend-test/output_bindings_fixes/`) and confirm no new failures
  - Ignore the known pre-existing failures listed at the top of this file (per repo steering)
  - Ask the user if questions arise

- [x] 5. On-hardware verification (USER-GATED - requires the NEXT LocalServer/JP6 build)
  - **VERIFIED 2026-08-04 on ryan-orin-nano (LocalServer arm64JP6 v1.0.49)**: triggered the deployed `bedrock_test` workflow (folder source `/aws_dda/yolotest`, 2 JPEGs) via `POST /workflows/registrations/{id}/trigger`. Run completed; the run log shows `Consumed processed folder-source file: /aws_dda/yolotest/zidane.jpg.dda_decoded.png` and `Consumed processed folder-source file: /aws_dda/yolotest/zidane.jpg`; folder drained from `[horses.jpg, zidane.jpg]` -> `[horses.jpg]` (staged `.dda_decoded.png` also removed). Consumption on success + staged-PNG cleanup confirmed on device.
  - **BLOCKED ON**: the next LocalServer build containing this fix (a JP6 build for other fixes is currently in flight; this change rides the one after)
  - Ask the user to run: place 3+ JPEGs in the folder source location on the JP6 (e.g. `/aws_dda/yolotest`), execute the deployed workflow repeatedly
  - Verify: each run processes the next-oldest image (folder drains in mtime order); no leftover `.dda_decoded.png` staging files; a corrupt image is relocated to `{INFERENCE_RESULTS_DIR}/{workflow_id}/failed/` and does not wedge the folder; the run after the folder empties fails with "No .jpg/.jpeg image files found"
  - _Requirements: 2.1, 2.2, 2.4, 2.5_

## Notes

- Build note: this is a device-side change in `src/backend/workflow_engine/` riding a LocalServer build. A JP6 build for other fixes is currently in flight — this fix goes in the NEXT build (task 5 is blocked until then).
- Known pre-existing test failures to ignore (per repo steering): IAM CDK-synth, cdk.out drift, portal workflow test-runner, `test_property_setup_command_wellformed` collection order, awsiot/panorama collection errors.
- Write exploration tests BEFORE implementing the fix, and run them on UNFIXED code to confirm the bug. Preservation tests follow the observation-first methodology (observe unfixed behavior, then encode it).
