# Implementation Plan

## Overview

This plan follows the bug-condition methodology. The bug is the **runtime** install of
the model conversion dependencies inside `create_virtual_env()` — on an offline device
the `pip install -r /dda_triton/model_conversion_requirements.txt` cannot reach a package
index and fails, leaving the deps absent. The fix bakes the pinned deps into all three
backend images at build time and converts `create_virtual_env()` to a verify-only step
that succeeds offline, while preserving every other behavior.

- **Property 1: Fix Checking** — for all inputs where `isBugCondition` is true (offline,
  deps not baked in, runtime pip install relied upon), the fixed setup completes with all
  pinned deps present and importable and performs NO runtime network install
  (Requirements 2.1, 2.2, 2.3).
- **Property 2: Preservation** — for all inputs where `isBugCondition` is false (online
  path, `cp_model_conversion_files`, existing `requirements.txt` install / build steps,
  pinned versions), `F(X) = F'(X)` (Requirements 3.1, 3.2, 3.3, 3.4).

> Property-based tests use [`hypothesis`](https://hypothesis.readthedocs.io/). If it is
> not already available in the backend test environment, add it as a test-only dependency
> before running the property tests below.

## Tasks

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Offline Runtime Pip Install Fails
  - **IMPORTANT**: Write this property-based test BEFORE implementing the fix
  - **CRITICAL**: This test MUST FAIL on the unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **GOAL**: Surface counterexamples that demonstrate the runtime `pip install` is
    attempted and fails when the device is offline and the deps are not baked into the image
  - **NOTE**: This test encodes the Expected Behavior (Property 1) - it will validate the
    fix when it passes after implementation
  - **Scoped PBT Approach**: The bug is deterministic. Scope the `hypothesis` strategy to
    the concrete failing domain: offline device (`networkAvailable == false`), deps NOT
    baked in (`depsBakedIntoImage == false`), requirements file present. Generate varied
    package-index host / error messages to mirror the real failure modes.
  - Create `test/backend-test/dda_triton/test_triton_setup_offline.py`
  - Exercise `dda_triton.triton_setup.create_virtual_env()` with `subprocess.check_call`
    mocked to raise `subprocess.CalledProcessError` simulating
    `[Errno -3] Temporary failure in name resolution` /
    `No matching distribution found for protobuf==4.25.8`
  - Assert the EXPECTED (fixed) behavior: for all offline bug-condition inputs,
    `create_virtual_env()` issues NO `pip install` / no network call and completes
    successfully with the pinned deps present (from Bug Condition `isBugCondition` and
    Expected Behavior `expectedBehavior` in design)
  - Run test on UNFIXED code
  - **EXPECTED OUTCOME**: Test FAILS (unfixed code still shells out to
    `python3 -m pip install -r /dda_triton/model_conversion_requirements.txt` and the
    mocked offline failure surfaces) - this is correct and proves the bug exists
  - Document counterexamples found (e.g. "create_virtual_env() invoked
    `pip install -r .../model_conversion_requirements.txt` and raised CalledProcessError
    offline instead of verifying baked-in deps")
  - Mark task complete when the test is written, run, and the failure is documented
  - _Requirements: 1.1, 1.2, 1.3_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Online Path, File Copy, and Pinned Versions Unchanged
  - **IMPORTANT**: Follow the observation-first methodology - capture behavior on the
    UNFIXED code first, then assert it is preserved
  - Add tests to `test/backend-test/dda_triton/test_triton_setup_offline.py` (or a sibling
    preservation test module)
  - Observe on UNFIXED code and record:
    - `cp_model_conversion_files()` copies `constants.py`, `model_config_pb2.py`,
      `model_autostart_utils.py` to the dda_triton destination and `model_convertor.py`,
      `convert_model_cleanup.py`, `model_conversion_requirements.txt` to the aws_dda
      destination, plus the resource files - to which destinations (Req 3.2)
    - `create_virtual_env()` with a NON-existent `requirements_file` logs and skips
      gracefully without raising (baseline edge case to preserve)
  - Write property-based tests (`hypothesis`):
    - For random destination-folder pre-states, the fixed `cp_model_conversion_files()`
      copies the SAME set of files/resources to the SAME destinations as observed (Req 3.2)
    - The baked-in / referenced pinned versions match `model_conversion_requirements.txt`
      exactly: grpcio==1.56.2, grpcio-tools==1.51.1, protobuf==4.25.8, requests==2.32.3,
      urllib3==2.2.3, scikit-learn==1.0.2, numpy==1.24.3 (plus setuptools, wheel, meson,
      opencv-python present) (Req 3.4)
    - For the online/deps-present path, setup completes successfully with all deps
      importable (Req 3.1)
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (confirms the baseline behavior to preserve)
  - Mark task complete when tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [ ] 3. Fix for offline model conversion dependency install

  - [x] 3.1 Bake model conversion deps into the standard image (`src/backend/Dockerfile`)
    - Add `COPY dda_triton/model_conversion_requirements.txt ./model_conversion_requirements.txt`
      alongside the existing `COPY requirements.txt ./`, before the late
      `COPY dda_triton ./dda_triton`
    - Add `RUN python3.11 -m pip install --no-cache-dir -r ./model_conversion_requirements.txt`
      after the existing backend `requirements.txt` install path (`prereqs_install.sh`)
    - Keep all other build steps and the existing `requirements.txt` install unchanged
    - _Bug_Condition: isBugCondition(input) where depsBakedIntoImage == false_
    - _Expected_Behavior: expectedBehavior(result) - pinned deps present and importable at startup_
    - _Preservation: existing requirements.txt install and other build steps unchanged (Req 3.3)_
    - _Requirements: 2.2, 3.3, 3.4_

  - [x] 3.2 Bake model conversion deps into the jp5 image (`src/backend/Dockerfile.jp5`)
    - Add `COPY dda_triton/model_conversion_requirements.txt ./model_conversion_requirements.txt`
    - Add `RUN pip install --no-cache-dir -r ./model_conversion_requirements.txt` after the
      existing `RUN pip install --no-cache-dir -r ./requirements.txt`
    - **setuptools<81 caveat**: order the install so it does NOT undo the deliberate
      `setuptools<81` pin required by `grpc_tools.protoc`; since the requirements list
      includes an unpinned `setuptools`, re-assert the `setuptools<81` cap after the install
      if necessary
    - _Bug_Condition: isBugCondition(input) where depsBakedIntoImage == false_
    - _Expected_Behavior: expectedBehavior(result) - pinned deps present, setuptools<81 preserved_
    - _Preservation: working jp5 image and existing build steps unchanged (Req 3.3)_
    - _Requirements: 2.2, 3.3, 3.4_

  - [x] 3.3 Bake model conversion deps into the jp6 image (`src/backend/Dockerfile.jp6`)
    - Add `COPY dda_triton/model_conversion_requirements.txt ./model_conversion_requirements.txt`
    - Add `RUN pip install --no-cache-dir -r ./model_conversion_requirements.txt` after the
      existing `RUN pip install --no-cache-dir -r ./requirements.txt`, applying the same
      `setuptools<81` ordering consideration as jp5
    - _Bug_Condition: isBugCondition(input) where depsBakedIntoImage == false_
    - _Expected_Behavior: expectedBehavior(result) - pinned deps present, setuptools<81 preserved_
    - _Preservation: working jp6 image and existing build steps unchanged (Req 3.3)_
    - _Requirements: 2.2, 3.3, 3.4_

  - [x] 3.4 Convert `create_virtual_env()` from install to verify-only (`src/backend/dda_triton/triton_setup.py`)
    - Remove the `subprocess.check_call(... pip install ...)` block - perform NO `pip install`
      and no network call at runtime
    - When the requirements file is present, parse package names and verify each
      distribution is already importable/locatable (e.g. `importlib.metadata.version` /
      `importlib.util.find_spec`); log success when all present, log a clear actionable
      error listing any missing packages (indicates a build-time regression, not a runtime
      network problem)
    - Keep the function signature and defaults (`python_path`, `requirements_file`) so
      `setup_triton()` and existing callers are unaffected
    - Continue to handle a missing requirements file gracefully (log and skip), matching
      today's behavior
    - Leave `cp_model_conversion_files()` and `setup_triton()` call order unchanged
    - _Bug_Condition: isBugCondition(input) where runtimePipInstall == true AND networkAvailable == false_
    - _Expected_Behavior: expectedBehavior(result) - verify (don't install), succeed offline with deps present_
    - _Preservation: cp_model_conversion_files and setup_triton call order unchanged (Req 3.2)_
    - _Requirements: 2.1, 2.3, 3.2_

  - [x] 3.5 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Offline Triton Setup Succeeds With Baked-In Dependencies
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - The test from task 1 encodes the expected behavior; passing confirms it is satisfied
    - Run the bug condition exploration test from task 1
    - **EXPECTED OUTCOME**: Test PASSES (`create_virtual_env()` issues no install, succeeds
      offline, all pinned deps present) - confirms the bug is fixed
    - _Requirements: 2.1, 2.2, 2.3_

  - [x] 3.6 Verify preservation tests still pass
    - **Property 2: Preservation** - Online Path, File Copy, and Pinned Versions Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run the preservation property tests from task 2
    - **EXPECTED OUTCOME**: Tests PASS (no regressions) - online path, `cp_model_conversion_files`,
      missing-file graceful skip, and pinned versions all preserved
    - **VERIFIED**: 8/8 tests pass (offline + preservation) under Python 3.9.18 on the
      Xavier NX device via `PYTHONPATH=src/backend python3 -m pytest
      test/backend-test/dda_triton/test_triton_setup_offline.py
      test/backend-test/dda_triton/test_triton_setup_preservation.py`
    - _Requirements: 3.1, 3.2, 3.3, 3.4_

- [ ] 4. Integration tests
  > **DEFERRED TO BUILD SERVER**: Tasks 4.1-4.3 require full multi-arch image builds
  > and container runs, which are too slow on the Xavier NX device. Run these on a more
  > powerful aarch64 build machine after pulling `origin/python_310`.
  - [ ] 4.1 Build each image variant and assert deps are baked in
    - Build `src/backend/Dockerfile`, `Dockerfile.jp5`, and `Dockerfile.jp6`
    - Assert the pinned model conversion dependencies are importable inside each built
      image (build-time bake-in works for all three variants), and the existing
      `requirements.txt` deps are still present
    - For jp5/jp6, assert `setuptools<81` is still in effect and `grpc_tools.protoc` works
    - _Requirements: 2.2, 3.3, 3.4_
  - [ ] 4.2 Run backend container offline and assert setup succeeds
    - Run the backend container with networking disabled
    - Assert `setup_triton` completes, `create_virtual_env` issues NO `pip install` / no
      network call, and all model conversion deps are importable (full offline flow)
    - _Requirements: 2.1, 2.3_
  - [ ] 4.3 Run backend container online and assert preservation
    - Run the backend container with networking enabled
    - Assert setup still completes and `cp_model_conversion_files` still copies the same
      files/resources to the same destinations (online preservation)
    - _Requirements: 3.1, 3.2_

- [ ] 5. Checkpoint - Ensure all tests pass
  - Run the full backend test suite plus the new exploration, preservation, and
    integration tests
  - Confirm Property 1 (fix) passes, Property 2 (preservation) passes, and no regressions
    are introduced; ask the user if questions arise
  - **PARTIAL (on-device)**: The exploration + preservation unit/property tests pass on
    the Xavier NX (8/8). `triton_setup.py` compiles cleanly and all three Dockerfiles
    report no diagnostics. The full backend suite and the image-build integration tests
    (4.1-4.3) remain to be run on the build server.

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "description": "Run on UNFIXED code: surface the offline pip-install counterexample (task 1 FAILS) and capture preservation baselines (task 2 PASSES). Independent of each other.", "tasks": ["1", "2"] },
    { "wave": 2, "description": "Apply the fix: bake model conversion deps into all three images at build time and convert create_virtual_env to verify-only. All four subtasks are mutually independent.", "tasks": ["3.1", "3.2", "3.3", "3.4"] },
    { "wave": 3, "description": "Fix Checking and Preservation Checking: re-run task 1 (now PASSES) and task 2 (still PASSES) on the fixed code.", "tasks": ["3.5", "3.6"] },
    { "wave": 4, "description": "Integration: build all three variants and assert deps baked in (depends on 3.1-3.3).", "tasks": ["4.1"] },
    { "wave": 5, "description": "Integration: offline and online container runs (depend on the code fix and built images).", "tasks": ["4.2", "4.3"] },
    { "wave": 6, "description": "Checkpoint: all tests green.", "tasks": ["5"] }
  ]
}
```

Visual summary of the critical path:

```
Task 1 (Property 1: Bug Condition exploration test - FAILS on unfixed code)
   │
   ▼
Task 2 (Property 2: Preservation tests - PASS on unfixed code, observation-first)
   │   (1 and 2 are independent; both run on UNFIXED code first)
   ▼
Task 3 (Fix)
   ├─ 3.1 Bake deps into Dockerfile (standard) ─┐
   ├─ 3.2 Bake deps into Dockerfile.jp5 ────────┤  (3.1, 3.2, 3.3, 3.4 independent)
   ├─ 3.3 Bake deps into Dockerfile.jp6 ────────┤
   ├─ 3.4 create_virtual_env -> verify-only ────┘
   │            │
   │            ▼
   ├─ 3.5 Verify Property 1 now PASSES   (depends on 3.4)
   │            │
   │            ▼
   └─ 3.6 Verify Property 2 still PASSES (depends on 3.4)
   │
   ▼
Task 4 (Integration tests)
   ├─ 4.1 Build all 3 variants, assert deps baked in   (depends on 3.1, 3.2, 3.3)
   ├─ 4.2 Offline container run, assert setup succeeds (depends on 3.1-3.4, 4.1)
   └─ 4.3 Online container run, assert preservation    (depends on 3.4, 4.1)
   │
   ▼
Task 5 (Checkpoint - all tests pass)
```

**Critical path:** 1 → 2 → 3.4 → 3.5 → 3.6 → 4.1 → 4.2/4.3 → 5

## Notes

- Tasks 1 and 2 MUST run on the UNFIXED code before any Task 3 change: task 1 is the
  exploration test and is EXPECTED to FAIL (the failure is the counterexample that
  confirms the bug); task 2 captures preservation baselines that must PASS on the unfixed
  code. Do not "fix" task 1 when it fails.
- Subtasks 3.1, 3.2, 3.3, and 3.4 are mutually independent and may be done in any order or
  in parallel. The Dockerfile changes (3.1-3.3) carry the `setuptools<81` ordering caveat
  for jp5/jp6; the `create_virtual_env` change (3.4) must not perform any runtime network
  install.
- Tasks 3.5 and 3.6 re-run the SAME tests from tasks 1 and 2 against the fixed code — task
  1 must now PASS (fix checking) and task 2 must still PASS (preservation). Do not author
  new tests in these steps.
- Task 4 (integration) depends on the Dockerfile changes (3.1-3.3) being built and the
  code fix (3.4) being in place.
