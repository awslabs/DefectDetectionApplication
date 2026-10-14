# Implementation Plan: camera-grab-lock-leak

## Overview

The fix is a one-function change in `src/backend/utils/camera_manager.py`: `get_camera_frame` takes `get_frame_lock` in a `with` block. The tests follow the repo's bugfix method:

1. a bug-condition exploration test that fails on the unfixed code;
2. preservation tests recorded on the unfixed code;
3. the fix, after which both pass.

Device verification comes before the commit.

**Preservation-tracked file.** `camera_manager.py` is rebaselined in `iam_out_of_scope_baseline.json` from `3a2b05a8` to `4cd46b92`, with a note entry. Nothing else tracked changes.

## Tasks

- [x] 1. Write the bug-condition exploration test
  - **Property 1: Bug Condition.** A failed lazy open leaves the camera lock held.
  - `test/backend-test/camera_lifecycle/test_camera_grab_lock_release.py`:
    - `test_failed_open_raises_todays_exception_and_frees_the_lock` (Hypothesis);
    - `test_thor1_failed_open_does_not_block_another_cameras_grab`, through the real `connect_camera`;
    - `test_repeated_failed_opens_leave_no_lock_behind`.
  - **Outcome on the unfixed code:** all 3 FAILED.
    - Counterexample: `camera_id='0', failure='aravis_exception'`, with the lock still owned by the grab's worker.
    - A working camera's grab on another thread hung.
  - Correction to the test itself: the first version let the worker thread exit, and it passed on the unfixed code, because the next thread can reuse a dead thread's id and the lock then treats it as its owner. The workers now stay parked, like API workers in their pool.
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 2.1, 2.2, 2.3_

- [x] 2. Write the preservation tests before the fix
  - **Property 2: Preservation.**
    - `test_calls_outside_the_bug_condition_behave_as_today` (Hypothesis): cached and connect-then-grab calls, with a frame, with no frame, and with a grab that raises. The values were recorded on the unfixed code.
    - `test_static_cameras_still_bypass_the_lock`.
    - `test_grab_still_holds_the_lock_while_it_runs`.
  - **Outcome on the unfixed code:** all 3 PASSED.
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5_

- [x] 3. Fix
  - [x] 3.1 `get_camera_frame` takes `get_frame_lock` with `with`, covering the lazy open, the missing-camera raise and the grab. The inner `try`/`except` still wraps only the grab.
  - [x] 3.2 Rebaseline `camera_manager.py` in `iam_out_of_scope_baseline.json` from `3a2b05a8` to `4cd46b92`.
  - [x] 3.3 The exploration test now passes, and the preservation tests still pass: `camera_lifecycle` 15 passed, which includes the existing serialization tests.
  - _Requirements: 2.1, 2.2, 2.3, 3.1–3.5_

- [x] 4. Checkpoint, in the arm64 CPU flask-app test image with `HYPOTHESIS_PROFILE=ci`
  - `camera_lifecycle`: 15 passed.
  - `utils/test_camera_manager.py`: 11 passed.
  - `static_image_camera`: 125 passed and 1 failed. The failure is the pre-existing `RLock.locked()` one, identical on base.
  - `static_video_camera`: 130 passed.
  - `camera_sync`: 94 passed.
  - `security`: 233 passed and 15 skipped.
  - Guard pair: 4 passed and 3 skipped. IAM out-of-scope guard: 2 passed.

- [x] 5. Build and verify on devices (`.kiro/steering/builds.md`)
  - [x] 5.1 Pre-build checks:
    - no build running;
    - `cdk.out` absent;
    - no Portal stack in progress;
    - guards green.
  - [x] 5.2 Build and publish JP5 (this host), amd64 (x86 build server), JP6 and JP7 (fleet), one build per variant, from snapshot `wip/camera-grab-lock-leak-verify` (`58efef0`), which is not pushed.
    - Published: JP5 1.0.49, amd64 1.0.44, JP6 1.0.72, JP7 1.0.49.
  - [x] 5.3 Deploy to the MIC-730, the Orin, thor1 and the Dell by revising only the LocalServer version. On thor1, restart the Triton models after the backend restarts.
  - [x] 5.4 On thor1 and the Orin:
    - preview the stale Image_Source whose camera is on the other device; the answer is the same 500 as before;
    - preview a working camera from 8 threads at once; all return 200;
    - repeat the pair 3 times.
    - Outcome: 8 rounds on each device. Every failing preview answered 500 in about 1 s, and every concurrent request completed.
    - thor1: 44 of 44 answered 200.
    - The Orin: 40 of 44 answered 200. The 4 answered 500 because of a pre-existing preview-file race, not the lock (see `verification-notes.md`).
  - [x] 5.5 On the MIC-730 and the Dell, inside the deployed backend image with real Aravis:
    - grab a missing camera id; the error is unchanged;
    - grab `Fake_1` on another thread; it succeeds and the lock is free.
    - Outcome: 3 of 3 on each device.
  - [x] 5.6 Soak each device for 30 minutes: the backend stays healthy, with no restart. Leave every device as it was found.
    - Outcome: 0 health failures and no unexpected restart. thor1 restarted once at start-up, before the checks, as after earlier deployments. Every device was left as found.
  - [x] 5.7 Write `verification-notes.md`.

- [x] 6. Commit, with the user's go-ahead
  - Commit on `fix/camera-grab-lock-leak`, stating what was verified on which device and the rebaselined hash. Merge into `integration/all-specs` as the user directs.
  - Committed on `fix/camera-grab-lock-leak` on top of `integration/all-specs` `385fe43`, then fast-forwarded into `integration/all-specs` and pushed to origin on 2026-09-28, as the user directed. The code is identical to the verified snapshot `58efef0`.
