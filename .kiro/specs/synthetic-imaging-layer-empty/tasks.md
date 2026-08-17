# Implementation Plan

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - Synthesized Imaging Layer Asset Contains PIL
  - **CRITICAL**: This test MUST FAIL on unfixed code - failure confirms the bug exists
  - **DO NOT attempt to fix the test or the code when it fails**
  - **NOTE**: This test encodes the expected behavior - it will validate the fix when it passes after implementation
  - **GOAL**: Surface the counterexample: the staged imaging layer asset lacks `python/PIL`
  - **Scoped PBT Approach**: CDK synthesis is deterministic — the synth-level assertion over the staged asset is the exhaustive check for the bug condition
  - Add `edge-cv-portal/infrastructure/test/synthetic-imaging-layer-empty.test.ts`: synthesize the SyntheticDataStack, resolve the `SyntheticImagingLayer` staged asset path from the `aws:asset:path` metadata, assert the staged asset contains `python/PIL` (from Bug Condition `isBugCondition` in design)
  - Run test on UNFIXED code (jest in `edge-cv-portal/infrastructure`)
  - **EXPECTED OUTCOME**: Test FAILS (asset holds only `build.sh` + `requirements.txt` — proves the bug exists)
  - Document counterexamples found
  - _Requirements: 1.1, 1.2, 2.1_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Sibling Layers, Handler Wiring, and Manual Build Unchanged
  - **IMPORTANT**: Follow observation-first methodology
  - Observe on UNFIXED code: shared/jwt layer assets are verbatim copies of `backend/layers/shared` and `backend/layers/jwt`; handler has exactly 3 layers, runtime python3.11, handler `synthetic_data.handler`, MemorySize 1024, Timeout 900, env var set
  - Write preservation tests in the same test file capturing those observations (from Preservation Requirements in design)
  - Run tests on UNFIXED code
  - **EXPECTED OUTCOME**: Preservation tests PASS (confirms baseline behavior to preserve)
  - _Requirements: 3.1, 3.2_

- [x] 3. Fix for the empty SyntheticImagingLayer asset

  - [x] 3.1 Implement the fix
    - In `edge-cv-portal/infrastructure/lib/synthetic-data-stack.ts`, replace the plain `lambda.Code.fromAsset(.../layers/imaging)` for `SyntheticImagingLayer` with a bundled asset: `bundling.image: lambda.Runtime.PYTHON_3_11.bundlingImage`, command `pip install -r requirements.txt -t /asset-output/python`
    - Add a local bundling fallback (`bundling.local.tryBundle`) that runs pip on the host with the same manylinux wheel targeting as `build.sh` (`--platform manylinux2014_x86_64 --implementation cp --python-version 3.11 --only-binary=:all:`) into `<outputDir>/python`; return false on failure so CDK falls back to Docker
    - Update the layer comment; keep `build.sh` unchanged for manual builds
    - _Bug_Condition: isBugCondition(input) — staged imaging layer asset lacks python/PIL_
    - _Expected_Behavior: synthesized imaging layer asset always contains python/PIL (Property 1)_
    - _Preservation: shared/jwt layer staging, handler wiring, build.sh manual path (Property 2)_
    - _Requirements: 2.1, 2.2, 3.1, 3.2, 3.4_

  - [x] 3.2 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - Synthesized Imaging Layer Asset Contains PIL
    - **IMPORTANT**: Re-run the SAME test from task 1 - do NOT write a new test
    - **EXPECTED OUTCOME**: Test PASSES (confirms the staged asset now contains python/PIL)
    - _Requirements: 2.1, 2.2_

  - [x] 3.3 Verify preservation tests still pass
    - **Property 2: Preservation** - Sibling Layers, Handler Wiring, and Manual Build Unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - _Requirements: 3.1, 3.2_

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the full infrastructure jest suite in `edge-cv-portal/infrastructure`; all 94 pre-existing tests plus the new spec tests must pass
  - _Requirements: 3.3_

- [x] 5. Deploy and live-verify the fix
  - Follow `.kiro/steering/builds.md`: confirm no component build is running (`pgrep -af "gdk component build"` / `build-custom.sh`); move `cdk.out` aside if needed
  - `cdk deploy` the SyntheticDataStack from the worktree (account 164152369890, us-east-1)
  - Verify a NEW imaging layer version is attached to `dda-synthetic-data-handler` (`aws lambda get-function-configuration`)
  - Verify the deployed layer content contains `python/PIL` (`aws lambda get-layer-version` + zip inspection)
  - Direct Lambda invoke exercising the PIL import path with real user claims (user_id `a4b804e8-5061-7004-12f2-38a0149dcd4c`, usecase `645504ce-a60a-4009-8349-7548c0025cd3`, bucket `ryvan-cookies`) succeeds with no `No module named 'PIL'`
  - _Requirements: 2.2, 3.2_
