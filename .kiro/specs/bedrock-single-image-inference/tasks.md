# Implementation Plan

## Overview

This plan fixes the Bedrock two-image requirement using the exploratory bugfix workflow: reproduce the defect first (Property 1: Bug Condition — single available primary image is sufficient), capture existing behavior (Property 2: Preservation — two-image and no-primary behavior unchanged), apply the fix (optional reference frame in `_run_one`), then validate. Device-side change in `src/backend/workflow_engine/` — rides a LocalServer build for on-device use, but the executor path is fully unit-testable.

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
      "description": "Checkpoint - run the workflow_engine suite, no new failures. Depends on wave 2."
    }
  ]
}
```

- Tasks 1 and 2 are independent and must be completed BEFORE task 3 (tests written against unfixed code).
- Task 3 depends on 1 and 2. Sub-tasks 3.2 and 3.3 depend on 3.1.
- Task 4 depends on 3.

## Tasks

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Single available primary image is sufficient
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the defect exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **Scoped PBT Approach**: deterministic defect - concrete cases over the three reference-unavailable shapes; the harness in `test/backend-test/workflow_engine/test_workflow_bedrock_inference.py` (RecordingInvoker, make_document, tmp_path frames) is the pattern to follow
  - Create `test/backend-test/workflow_engine/test_bedrock_single_image_exploration.py`
  - Test cases (from isBugCondition in design): `capturePaths.reference` is None (compiler's unfed-port shape); `reference` key absent entirely; reference path present but file missing on disk — in each case the `in` frame exists and is readable, and the test asserts `process()` succeeds, the invoker was called exactly once with images == [("Input image", <in bytes>)], and the parsed answer is merged into the metadata
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (BedrockInferenceError raised for the reference in all three cases - this proves the defect)
  - Document counterexamples found
  - Mark task complete when test is written, run, and failure is documented
  - _Requirements: 1.1, 1.2, 1.3_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Two-image and no-primary behavior unchanged
  - **IMPORTANT**: Follow observation-first methodology - observe UNFIXED behavior first, then encode it
  - Create `test/backend-test/workflow_engine/test_bedrock_single_image_preservation.py`
  - Observe on UNFIXED code: both-frames invoker call shape (model, prompt, both labeled pairs in order, region, max_tokens) and merged metadata; missing/unfed `in` frame raises BedrockInferenceError with the node id; a raising invoker surfaces BedrockInferenceError with the node id; documents without bedrock bindings pass tag_values through
  - Write property-based tests (Hypothesis, per repo convention): for any model/prompt/region/max_tokens parameters and frame bytes, the both-frames invoker call and merged metadata match the recorded unfixed shape; the no-primary cases (in path None, in key absent, in file missing) always raise with the node id
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 3. Fix the two-image requirement in BedrockInferenceProcessor

  - [x] 3.1 Implement the fix in `src/backend/workflow_engine/output_bindings.py`
    - In `_run_one`, split frame collection: `in` port unchanged (missing path or unreadable file raises BedrockInferenceError with today's exact messages); `reference` port becomes optional — path absent/None → log a warning ("reference port not fed; performing single-image inference") and continue; file unreadable → log a warning with the OSError detail and continue
    - Invoker call site unchanged (images has one or two pairs; `_default_bedrock_invoker` already handles any list length)
    - Update the two legacy tests that encode the OLD behavior (per design Test Seam): `test_workflow_bedrock_inference.py::TestBedrockInferenceProcessor::test_missing_captured_frame_fails_with_the_node` and `::test_unfed_port_fails_with_the_node` — both become single-image success cases (or are re-pointed at the `in` port to keep their original intent)
    - Leave untouched: `bindings()`, `process()` error surfacing, `parse_bedrock_answer`, `_default_bedrock_invoker`, the compiler, and the portal
    - _Bug_Condition: isBugCondition(run) - in frame available, reference frame unavailable, from design_
    - _Expected_Behavior: Property 1 - single-image invocation with logged omission, from design_
    - _Preservation: Property 2 - two-image byte-identical; no-primary and invoker-failure surfacing unchanged_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4_

  - [x] 3.2 Verify bug condition exploration test now passes
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - Run `python -m pytest test/backend-test/workflow_engine/test_bedrock_single_image_exploration.py`
    - **EXPECTED OUTCOME**: Test PASSES (confirms the defect is fixed)
    - _Requirements: 2.1, 2.2_

  - [x] 3.3 Verify preservation tests still pass
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run `python -m pytest test/backend-test/workflow_engine/test_bedrock_single_image_preservation.py`
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [x] 4. Checkpoint - Ensure all tests pass
  - Run `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/` and confirm no new failures (includes the updated legacy bedrock tests and the opcua preservation suite that exercises bedrock failure surfacing)
  - Ignore the known pre-existing failures listed in the repo steering
  - Ask the user if questions arise

## Notes

- Build note: device-side change in `src/backend/workflow_engine/` — on-device behavior rides the NEXT LocalServer build (alongside the folder-source-image-consumption fix). No portal/compiler change: the compiler already emits `capturePaths.reference = None` for unfed reference ports.
- Two existing tests assert the defect and are updated as part of task 3.1 (design Test Seam) — updating them earlier would break the test-first ordering.
- Known pre-existing test failures to ignore (per repo steering): IAM CDK-synth, cdk.out drift, portal workflow test-runner, `test_property_setup_command_wellformed` collection order, awsiot/panorama collection errors.
