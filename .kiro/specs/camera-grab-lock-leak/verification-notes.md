# Camera Grab Lock Leak: Verification Notes

Date: 2026-09-28, all times UTC.

## Before the fix, on hardware

On jetson-thor1 (`LocalServer.arm64JP7` 1.0.48, 00:50Z):
- The Basler `Basler-267601652282-23405186` was on a USB 2 link, and Aravis failed to open it with "Failed to bootstrap USB device".
- Three previews of Image_Source `o70qz7ci` answered HTTP 500 in 184, 43 and 41 ms.
- Each preview went through the lazy open in `get_camera_frame`, which acquires the lock and never releases it when the open fails.
- The backend container was restarted at 01:02Z to release the lock.

## Local and container checks

The checks ran in the arm64 CPU flask-app test image.

**Exploration test on the unfixed code:** 3 FAILED, as expected.
- The first counterexample was `camera_id='0', failure='aravis_exception'`. The lock was reported as `<locked _thread.RLock ... count=1>`, owned by the grab's worker.
- A working camera's grab on another thread hung.

**Preservation tests on the unfixed code:** 3 PASSED.

**After the fix:**

| Suite | Result |
|---|---|
| `camera_lifecycle` (new tests plus the existing serialization tests) | 15 passed |
| `utils/test_camera_manager.py` | 11 passed |
| `static_image_camera` | 125 passed, 1 failed (the pre-existing `RLock.locked()` failure) |
| `static_video_camera` | 130 passed |
| `camera_sync` | 94 passed |
| `security` | 233 passed, 15 skipped |
| Guard pair, on the host | 4 passed, 3 skipped |
| IAM out-of-scope guard, on the host | 2 passed |

`camera_manager.py` was rebaselined from `3a2b05a8` to `4cd46b92`.

## Builds

The builds used snapshot `wip/camera-grab-lock-leak-verify` = `58efef0`: `integration/all-specs` `34abf41` plus the three fix files, which are byte-identical to the working tree. The snapshot went to the fleet clones as a git bundle, and the amd64 build used a `git archive` of it. Nothing was pushed.

The four builds ran in parallel, one per variant, each on its own host and checkout, as the revised `builds.md` allows.

| Target | Where | Result | Published |
|---|---|---|---|
| JP5 | this host | build 01:46–02:13, in-build gates passed; publish until 02:21 | `arm64JP5` **1.0.49** |
| amd64 | x86 build server | 01:46–01:51 | `amd64` **1.0.44** |
| JP6 | fleet job `ab721bda`, JP6 Build Server | "Source synced to 58efef0"; 01:47–02:05 | `arm64JP6` **1.0.72** |
| JP7 | fleet job `78536f40`, `Jp7-24.04-pro` | "Source synced to 58efef0"; 01:47–02:08 | `arm64JP7` **1.0.49** |

Each fleet job was submitted by a temporary Cognito principal, which was removed right after submission. None remain.

## Devices

Each deployment revision changed only the LocalServer version:
- thor1 `40d2ee06`;
- Orin `cf8126b2`;
- MIC-730 `83f65438`;
- Dell `ea38fac7`.

Nothing was previewed on the unfixed versions.

**thor1 and the Orin: the real endpoints.** Each device has a stale Image_Source for the Basler that is now attached to the other device, so a failing open happens naturally.

A round is:
1. one preview of the stale Image_Source;
2. then 8 concurrent previews of a working camera, or 4 during the soak.

There were 3 rounds right after the rollout and 5 more during the soak.

| Device | Failing preview | Concurrent previews of a working camera | Soak |
|---|---|---|---|
| thor1, JP7 1.0.49 | `28183exv`: 8 of 8 answered 500 in about 1.02 s ("Device ... not found") | Basler `o70qz7ci`: 44 of 44 answered 200. The slowest took 6.15 s: the first round after the restart includes pipeline start-up; later rounds took 0.6–2.8 s, one Basler grab at a time | 29 of 29 single grabs answered 200; 0 of 57 health checks failed |
| Orin, JP6 1.0.72 | `t7j8ipuz`: 8 of 8 answered 500 in about 1.04 s | Fake camera `lyn5mwtf`: 40 of 44 answered 200 and 4 answered 500, all within 2.42 s. The 500s are a separate, pre-existing preview-file race (see Observations), not the lock | 29 of 29 single grabs answered 200; 0 of 57 health checks failed |

With the unfixed code, every concurrent preview served by a different worker thread would have hung. Here, every request completed.

**MIC-730 and the Dell: inside the deployed backend image, with real Aravis.** Neither device has a camera Image_Source. `docker exec` ran this sequence against the deployed image's `camera_manager`:
1. a grab of `Basler-000000MISSING-00000000`, on a worker thread that stays alive;
2. a grab of `Fake_1` on another thread;
3. a check that the main thread can take the lock.

It ran three times on each device: once after the rollout, then twice during the soak.

| Device | Missing camera | `Fake_1` on another thread | Lock free | Soak |
|---|---|---|---|---|
| MIC-730, JP5 1.0.49 | `AravisCameraException`: "Device 'Basler-000000MISSING-00000000' not found (6)", 3 of 3 | 512×512 frame in 0.066–0.083 s, 3 of 3 | 3 of 3 | 0 of 58 health checks failed |
| Dell, amd64 1.0.44 | the same, 3 of 3 | 512×512 frame in 0.036–0.062 s, 3 of 3 | 3 of 3 | 0 of 58 health checks failed |

**Backend health across the rollout and soak.**
- The Orin, the MIC-730 and the Dell each kept one container with 0 restarts and no OOM.
- thor1's new container restarted once at start-up, at 02:27:44Z, before the checks. That is the same clean self-restart seen after its 1.0.47 and 1.0.48 deployments. The count stayed at 1 through the soak, with no OOM.

**Left as found.**
- Models are the same as before on every device: thor1 9 of 9 READY, which needed a manual start after the restart; the Orin 3 Triton models READY with its vLLM model FAILED as before; the MIC-730 3 of 3; the Dell 4 of 4.
- Static pins are unchanged: thor1 `ppe-hse-factory.png`, the Orin `zidane.jpg`, nothing pinned on the MIC-730 or the Dell.
- The test scripts were removed from each device, and the login files are shredded.

## Observations (pre-existing, not caused by this change)

- **Concurrent previews of one Image_Source can clobber each other.** Every preview of an Image_Source writes the same file, `/aws_dda/image-capture/preview/default_file_prefix-{id}.jpg`. When two previews overlap, one can delete or overwrite the other's file.
  - The response is then 500, with "No such file or directory: …/default_file_prefix-lyn5mwtf.jpg" or "Captured image was corrupted and has been deleted".
  - Reproduced on the Orin: 2 of 8 concurrent previews failed in two of three direct tries. The failures come after the camera grab, which completed.
  - thor1's Basler previews did not show it, because each preview waits about 0.3 s for the camera lock, so their pipelines rarely overlap.
  - The live UI previews one camera at a time, so users are unlikely to hit it. It could be a follow-up.
- **The camera connect route cannot test a camera that is already open.** `GET /cameras/{id}/connect` opens a second handle, so it answers `LIBUSB_ERROR_BUSY` when the backend already has the camera open, which it does at start-up for configured cameras. Seen on thor1.
