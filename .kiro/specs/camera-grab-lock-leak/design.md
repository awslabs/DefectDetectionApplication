# Design Document

## Overview

`get_camera_frame` takes the camera lock in a `with` block that covers the lazy open, the missing-camera check and the grab. The block releases the lock on every exit, including the two raises that leak it today. Nothing else changes: the same exceptions reach the same callers, cached cameras are reused, and static cameras are still served before the lock.

## Root Cause

```python
get_frame_lock.acquire()                      # held from here
if camera_id not in camera_objects:
    connect_camera(camera_id)                 # raises -> lock never released
camera = camera_objects.get(camera_id)
if camera is None:
    raise Exception("Camera not able to connect for ID ...")   # lock never released
try:
    ...grab...
finally:
    get_frame_lock.release()                  # only covers the grab
```

`connect_camera` takes the same lock itself, with a `with` block, and releases it on its own exit. Because the lock is re-entrant, that inner release only drops the nested hold. The outer `acquire()` stays held.

## Fix

```python
with get_frame_lock:
    if camera_id not in camera_objects:
        logger.error("Attempting to create camera object")
        connect_camera(camera_id)
    camera = camera_objects.get(camera_id)
    if camera is None:
        logger.error(f"Camera not found for ID {camera_id}")
        raise Exception(f"Camera not able to connect for ID {camera_id}")
    try:
        frame = _get_camera_frame(camera_id, camera, camera_config)
        if frame is not None:
            return frame
        else:
            raise Exception(f"Unable to get camera frame for camera id: {camera_id}")
    except Exception:
        raise Exception(f"Unable to get camera frame for camera id: {camera_id}")
```

- **Same exceptions.** The inner `try`/`except` still wraps only the grab. A failed open therefore raises the same exception object as today, and a failed grab raises today's `"Unable to get camera frame for camera id: …"`.
- **Same serialization.** The lock is held for the same span, open plus grab, and stays re-entrant, so `connect_camera`'s nested `with get_frame_lock` still works.
- **Rejected: an early `release()` before each raise.** It repeats the release on several paths and is easy to miss on the next change. A `with` block is the pattern `connect_camera`, `disconnect_camera` and `disconnect_all_cameras` already use.

`src/backend/utils/camera_manager.py` is preservation-tracked. Its hash in `test/backend-test/security/baselines/iam_out_of_scope_baseline.json` is rebaselined from `3a2b05a8` to `4cd46b92`, with a note entry. The diff contains only this change, and there is no IAM change.

## Testing Strategy

The tests are in `test/backend-test/camera_lifecycle/test_camera_grab_lock_release.py` and use Hypothesis with the repo profiles.

**How each check is set up.**
- The grab runs on a worker thread that stays alive afterwards, like an API worker back in its pool. The lock is then requested from another thread.
- Both halves are needed:
  - the owner can re-enter the lock;
  - a worker that has exited can have its thread id reused by the next thread, which the lock then treats as its owner.
- The first version of the tests let the worker exit. It passed on the unfixed code for exactly that reason.
- Each test swaps in a fresh `RLock`, so a leak poisons only that test's lock.

**Property 1, the bug condition.** It covers a failed open (an `AravisCameraException`, another exception, or no camera object registered) and the thor1 case end to end through the real `connect_camera`. It checks that the exception is unchanged and that the lock is free for another thread. On the unfixed code it failed: the first counterexample was `camera_id='0', failure='aravis_exception'`, with the lock still owned. A working camera's grab on another thread then hung.

**Property 2, preservation.** It covers these cases, with values recorded on the unfixed code, where the tests passed:
- cached and connect-then-grab calls, successful or with no frame;
- a grab that raises;
- static cameras still served while another thread holds the lock;
- a grab in progress still holding the lock.

**On devices** (required by `.kiro/steering/builds.md`):
- **thor1 and the Orin.** Each has a stale Image_Source for the Basler that is attached to the other device, so the failing open happens naturally: thor1's `28183exv` and the Orin's `t7j8ipuz`. Preview it, then preview a working camera from several threads at once: thor1's Basler `o70qz7ci`, and the Orin's Aravis Fake camera `lyn5mwtf`. Every working preview must return 200.
- **MIC-730 and the Dell.** They have no camera Image_Source, so the same sequence runs inside the deployed backend image with real Aravis: a missing camera id, then `Fake_1` on another thread.
- **Every device.** A 30-minute soak with the backend healthy and no restarts.
