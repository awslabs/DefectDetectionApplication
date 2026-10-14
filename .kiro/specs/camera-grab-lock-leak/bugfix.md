# Bugfix Requirements Document

## Introduction

**A camera that cannot be opened leaves the device-wide camera lock held. Until the backend restarts, every open, grab or close of a physical camera from any other thread waits forever.**

**Where it was found.** On jetson-thor1 (`LocalServer.arm64JP7` 1.0.48) on 2026-09-28.
- The Basler acA4600-10uc `Basler-267601652282-23405186` had dropped to a USB 2 link, and Aravis could not open it: `Failed to bootstrap USB device '(null)-(null)-(null)-267601652282' (3)`.
- Three previews of its Image_Source `o70qz7ci` answered HTTP 500, in 184, 43 and 41 ms.
- The backend log shows each preview going through the lazy open in `get_camera_frame` and failing there, on the path that never releases the lock.
- The backend container was restarted to release it.

**Cause.** `utils/camera_manager.get_camera_frame` calls `get_frame_lock.acquire()` before the `try`/`finally` that releases the lock. Two raises sit between the two:
- the lazy `connect_camera(camera_id)`, which raises `AravisCameraException` when the device cannot be opened;
- `raise Exception("Camera not able to connect for ID …")`, when no camera object exists after the open.

Either one leaves the process-wide `RLock` owned by the calling thread. Because an `RLock` lets its owner re-enter, that thread's later requests still work; every other thread blocks.

**Who is affected.** Any device where a configured physical camera is missing, unplugged, on a bad link, or already held open. The first grab attempt wedges physical-camera access for the whole backend. Static cameras and Python sources never take the lock and are not affected.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN `get_camera_frame(camera_id, config)` is called for a physical camera that is not in `camera_objects` and `connect_camera(camera_id)` raises, THEN the exception reaches the caller with `get_frame_lock` still held by the calling thread.

1.2 WHEN `connect_camera` returns but registers no camera object, THEN `get_camera_frame` raises `Exception("Camera not able to connect for ID {camera_id}")` with the lock still held.

1.3 WHEN the lock has been left held, THEN every other thread blocks indefinitely if it calls any of these:
- `get_camera_frame` for a physical camera, which covers Image_Source previews and captures, workflow Frame_Feed grabs and digital-input grabs;
- `connect_camera`, `disconnect_camera` or `disconnect_all_cameras`.

Each blocked API request also ties up one worker thread, so a live preview polling every 500 ms uses up the pool one request at a time.

1.4 WHEN the owning thread, which is an API worker back in its pool, serves a later request, THEN that request re-enters the lock and succeeds. This makes the defect look intermittent.

### Expected Behavior (Correct)

2.1 WHEN the lazy open in `get_camera_frame` raises, THEN the caller SHALL receive the same exception object it receives today, and `get_frame_lock` SHALL be released before the exception leaves `get_camera_frame`.

2.2 WHEN no camera object is registered after the open, THEN the caller SHALL receive `Exception("Camera not able to connect for ID {camera_id}")` as today, and the lock SHALL be released.

2.3 After any failed grab, an open, grab or close of a physical camera on another thread SHALL proceed without a backend restart.

### Unchanged Behavior (Regression Prevention)

3.1 A successful grab SHALL return the same frame dict as today, reuse a cached camera without reconnecting, and pass the configuration through unchanged.

3.2 A missing frame, or a grab cycle that raises, SHALL still raise `Exception("Unable to get camera frame for camera id: {camera_id}")`.

3.3 Opens, grabs and closes SHALL stay serialized by `get_frame_lock`, which is the LIBUSB_ERROR_BUSY protection of `camera_lifecycle/test_camera_open_serialization.py`. The lock SHALL stay re-entrant.

3.4 The Static_Image_Camera and the Static_Video_Camera SHALL still be served before the lock is taken.

3.5 The signature `get_camera_frame(camera_id, camera_config=None)` SHALL be unchanged.

## Bug Condition and Property Specification

### Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type GrabCall { cameraId, config, openOutcome }
  OUTPUT: boolean

  // A physical-camera grab whose lazy open fails: the open raises, or it
  // returns without registering a camera object.
  RETURN X.cameraId NOT IN { 'static-image-camera', 'static-video-camera' }
         AND X.cameraId NOT IN camera_objects
         AND X.openOutcome IN { RAISES, RETURNS_WITHOUT_CAMERA }
END FUNCTION
```

### Property 1: Fix Checking

```pascal
FOR ALL X WHERE isBugCondition(X) DO
  raised := exception of get_camera_frame'(X.cameraId, X.config)
  ASSERT raised = exception of get_camera_frame(X.cameraId, X.config)
  ASSERT another thread can acquire get_frame_lock
END FOR
```

### Property 2: Preservation Checking

```pascal
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT get_camera_frame'(X) = get_camera_frame(X)   // same value, or same exception type and message
  ASSERT another thread can acquire get_frame_lock after the call
END FOR
```
