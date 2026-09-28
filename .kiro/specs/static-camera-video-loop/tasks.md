# Implementation Plan: static-camera-video-loop

## Overview

The work adds a second virtual camera, `static-video-camera`. It follows the image camera's patterns and does not edit the image camera's code paths. The order is:

1. **Video core**: `src/backend/utils/video_loop.py`, which is stdlib-only at import and imports `cv2` lazily. It holds the sniffer, `probe_video`, `loop_frame_index` and `VideoLoopPlayer`.
2. **Video store**: `StaticVideoStore` in `src/backend/utils/static_video_camera.py`.
3. **Device integration**:
   - enumeration (`aravis_functions`) and camera_manager short-circuits;
   - the Video_Pin_API router;
   - the inventory entry, the pin worker parameters and the agent's second worker;
   - the runtime inventory provider and the camera-binding capability family.
4. **Portal backend and infra**:
   - a vendored `video_loop.py`;
   - the slot-generic pin helpers, the video routes, and a probe in a child process with a timeout;
   - the parallel `VIDEO_PIN_REQUEST#` family and ingest routing;
   - the new `CameraVideoPinHandler` with a `VideoLayer`, which reuses the camera registry role.
5. **Portal frontend**: `StaticVideoPanel`, the API methods, the compatibility updates and the second picker shortcut.
6. **Verification**: every platform image, component builds, on-device end-to-end, and the Portal deploy.

Property tests implement the design's 12 properties with Hypothesis:

- one test per property, in its own module (named `test_property_video_*` / `test_video_*` to avoid basename collisions with the other device suites);
- tagged `**Feature: static-camera-video-loop, Property {N}: {property_text}**`;
- using the repo profiles (100 examples with `HYPOTHESIS_PROFILE=ci`).

Device clips are synthesized once per session with the image's `ffmpeg`.

Existing suites must keep passing unmodified: static image camera, camera_sync, workflow binding, and the Portal pin suites. The exceptions are the conscious updates called out below, where a test enumerates the complete set of compatible camera types, routes, or `MAX_REPORT_BYTES`.

Preservation-tracked files touched: `src/backend/app.py` and `src/backend/utils/camera_manager.py`. Both hashes are rebaselined in `test/backend-test/security/baselines/iam_out_of_scope_baseline.json`, with a note entry, in the same change (tasks 3.2 and 4.2). No Dockerfile, compose file, requirements file, recipe or IAM statement changes.

## Tasks

- [x] 1. Video core module
  - [x] 1.1 Create `src/backend/utils/video_loop.py`
    - Constants:
      - `SUPPORTED_VIDEO_CONTAINERS`, `SUPPORTED_VIDEO_CODECS`
      - `MAX_PIN_VIDEO_BYTES = 100 MiB`, `MAX_VIDEO_DIMENSION = 4096`, `MAX_VIDEO_FPS = 240.0`, `SNIFF_BYTES = 64`
      - `VideoValidationError`
    - `sniff_video_container(head)` recognizes these signatures:
      - ISO-BMFF `ftyp`: brand `qt  ` → MOV, any other brand → MP4
      - leading QuickTime `moov`/`wide`/`mdat` atom → MOV
      - `RIFF….AVI ` → AVI
      - EBML: `webm` DocType → WEBM, otherwise MKV
      - anything else → `None`
    - `normalize_codec(fourcc)`
    - `VideoInfo` and `probe_video(path)` (design Decision 5):
      - sniff, then open with `CAP_FFMPEG` and `CAP_PROP_ORIENTATION_AUTO=1`
      - reject with the limit named: fps outside `(0, 240]`, frame count < 1, width or height > 4096
      - frame 0 must decode
      - decode frame `n-1`, backing off up to `ceil(fps)` frames to find the exact count
      - messages: "not a supported video" plus the containers, or "could not be decoded" plus the codec and codec list
    - `loop_frame_index(now_ms, epoch_ms, fps, frame_count)` (Decision 3)
    - `VideoLoopPlayer(path, fps, frame_count)` (Decision 4):
      - serves the cached index, steps forward inside a `ceil(2·fps)` window, otherwise seeks
      - reopens once on a read failure
      - output: BGR→RGB contiguous bytes tagged `RGB`
      - `close()`
    - Only stdlib imports at module level
    - _Requirements: 1.2–1.6, 3.1–3.3, 3.5, 3.6, 3.8, 3.9, 10.3_

  - [x] 1.2 Create the shared clip library for tests
    - `test/backend-test/static_video_camera/video_clip_library.py` plus a session-scoped fixture in that directory's `conftest.py`
    - Uses `ffmpeg` with `testsrc2`, a few frames each, tiny sizes, to synthesize:
      - H.264 in MP4, MOV and MKV (the MKV with B-frames)
      - HEVC in MP4, MPEG-4 in MP4 at 30000/1001 fps, MJPEG in AVI, VP8 and VP9 in WebM
      - an AV1 clip
      - H.264 with rotation 90/180/270 (`-display_rotation` when the ffmpeg supports it, otherwise the `rotate` tag)
    - Reference RGB frames per clip come from one sequential decode; rotation references come from ffmpeg's autorotated first frame
    - Hand-built signature vectors go in `goldens/video_sniff_vectors.json` (shared with task 7.2)
    - Skip with an explicit reason only when `cv2` or `ffmpeg` is missing
    - _Requirements: 3.6, 10.1_

  - [x]* 1.3 Write property test for loop index arithmetic
    - **Property 1: Loop index arithmetic**
    - **Validates: Requirements 3.1, 3.2**
    - `test_property_video_loop_index.py`
    - Hypothesis over fps in (0, 240], frame counts ≥ 1, epochs and times (including before the epoch)
    - Checks: range, periodicity, monotonicity within a period, and the closed form

  - [x]* 1.4 Write property test for step/seek equivalence
    - **Property 5: Step/seek equivalence**
    - **Validates: Requirements 3.1, 3.3, 3.8**
    - `test_property_video_step_seek.py`
    - Library clips and index sequences: steps inside and beyond the window, backward jumps, wraps, repeats
    - Every frame equals its reference

  - [x]* 1.5 Write property test for rotation
    - **Property 9: Rotation honored**
    - **Validates: Requirements 3.6**
    - `test_property_video_rotation.py`
    - The served first frame equals ffmpeg's autorotated decode; the probe reports the displayed dimensions

- [x] 2. Video store
  - [x] 2.1 Create `src/backend/utils/static_video_camera.py`
    - `STATIC_VIDEO_CAMERA_ID`, `STATIC_VIDEO_CAMERA_IDENTITY` (design Decision 1), `StaticVideoPinError`, `StaticVideoUnavailableError`
    - `StaticVideoStore(base_dir=None, max_file_bytes=MAX_PIN_VIDEO_BYTES, clock=None)` over `$COMPONENT_WORK_PATH/static_video_camera/`, holding `pinned_video` and `pinned_video.json`; `get_store()` singleton
    - `pin_bytes`: size check, stage into `.tmp-*` and fsync, `probe_video`, then under the lock `os.replace` and an atomic sidecar write, then drop the caches and close the player. The staged file is removed on any failure
    - `pin_file(path, captures_root)`: the realpath/commonpath guard, not-found and size checks, then a stream-copy into staging and the same probe and commit
    - `status()`/`is_pinned()`: open, decode frame 0 and close, cached per media stat key; missing and undecodable data are logged separately
    - `unpin()`: "no video is pinned" error when empty; closes the player
    - `get_frame()`: `loop_frame_index(now)` through the per-instance player keyed on the media stat key. Failures raise `StaticVideoUnavailableError` naming `static-video-camera` and "no usable pinned video"
    - No `gi` imports
    - _Requirements: 1.1, 1.2, 1.5, 1.7–1.9, 3.1–3.10, 5.1–5.5, 7.1–7.3, 10.3, 10.4_

  - [x]* 2.2 Write property test for pin round trip and loop fidelity
    - **Property 2: Pin round trip and loop fidelity**
    - **Validates: Requirements 1.1, 1.2, 1.8, 3.1, 3.5**
    - `test_property_video_pin_round_trip.py`

  - [x]* 2.3 Write property test for cross-process consistency
    - **Property 3: Cross-process consistency**
    - **Validates: Requirements 3.4, 7.1, 7.2**
    - `test_property_video_cross_process.py`

  - [x]* 2.4 Write property test for invalid input
    - **Property 6: Invalid input preserves prior state**
    - **Validates: Requirements 1.3, 1.4, 1.5, 1.6, 1.9, 10.4**
    - `test_property_video_invalid_pin.py`
    - Inputs: non-video, sniffable garbage, truncated, AV1, oversize (injected limit), and stubbed-probe fps/dimension/frame-count violations
    - The error wording, unchanged prior state, and no `.tmp-*` leftovers

  - [x]* 2.5 Write property test for replace atomicity and decoder release
    - **Property 7: Replace atomicity and decoder release**
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.5, 10.3, 10.4**
    - `test_property_video_replace_atomicity.py`

  - [x]* 2.6 Write property test for corruption containment
    - **Property 8: Corruption containment**
    - **Validates: Requirements 3.10, 7.3**
    - `test_property_video_corruption_containment.py`

- [x] 3. Enumeration and camera_manager integration
  - [x] 3.1 `edge_ml1_p_camera_management/aravis_functions.py`
    - `getCameras()` appends `Camera(**STATIC_VIDEO_CAMERA_IDENTITY)` in its own `try/except` while the video store is pinned
    - `getCamera()` returns a video handle when pinned, else `AravisCameraNotFound`
    - The image branches are untouched
    - _Requirements: 2.1–2.6_
  - [x] 3.2 `utils/camera_manager.py`: video-id branches next to the image ones
    - Entry points: `get_camera_status`, `connect_camera`, `disconnect_camera`, `get_camera_feature_bounds`, `apply_camera_features`, `get_camera_frame`
    - All sit before `get_frame_lock` and `camera_objects`, and wrap `StaticVideoUnavailableError` in the existing `Exception` contract
    - Rebaseline `camera_manager.py` in `iam_out_of_scope_baseline.json` with a note
    - _Requirements: 3.7, 3.10, 4.2–4.5, 4.9_
  - [x]* 3.3 Write property test for interval idempotence and config invariance
    - **Property 4: Interval idempotence and acquisition-config invariance**
    - **Validates: Requirements 3.3, 3.7**
    - `test_property_video_interval_idempotence.py`, through `camera_manager` with the existing `mock_gi` pattern
  - [x]* 3.4 Write example tests for the short-circuits
    - The static-video grab never touches `camera_objects`, `connect_camera` or `get_frame_lock`
    - Status, connect and features behave as for the image camera
    - An unpinned grab raises naming the camera
    - A preview/capture request passes the loop frame to the mocked GStreamer executor
    - _Requirements: 3.10, 4.2, 4.3, 4.9_

- [x] 4. Video_Pin_API
  - [x] 4.1 Create `src/backend/endpoints/static_video_camera.py`
    - `POST /static-video-camera/pin`: multipart `file` capped at 100 MB + 64 KiB, or JSON `capturedImagePath`
    - `GET` and `DELETE`
    - Reuses the image endpoint module's multipart parser and captured-roots guard by import
    - Returns `{cameraId, metadata}` / `{cameraId, pinned: false}`; errors are 400 with the store message
    - _Requirements: 1.1–1.9, 5.3, 5.4_
  - [x] 4.2 Register the router in `src/backend/app.py`, alongside the static image router
    - Rebaseline `app.py` in `iam_out_of_scope_baseline.json` with a note
    - _Requirements: 1.1_
  - [x]* 4.3 Write endpoint tests (`test_video_endpoints.py`, FastAPI `TestClient`)
    - Happy paths and every error path
    - Pin-by-reference under a temporary captures root and a traversal rejection
    - Removal with nothing pinned
    - Image endpoints unaffected when both cameras are pinned
    - _Requirements: 1.1–1.9, 5.3, 5.4, 6.2_

- [x] 5. Camera sync, workflow runtime and binding
  - [x] 5.1 `camera_sync/inventory.py`
    - Add `TYPE_STATIC_VIDEO`, `_is_static_video_aravis_camera` (excluded by `camera_id` in every pin state), `_static_video_identity`, `_static_video_entry` and `_static_video_absent_entry`
    - Add the keyword parameters `static_video_pinned`, `static_video_metadata` and `static_video_absent_since`
    - The image code is untouched
    - _Requirements: 4.6, 4.7, 6.2_
  - [x] 5.2 `camera_sync/pin_worker.py`
    - New constructor parameters, all defaulting to current behavior: `section_name`, `max_download_bytes`, `no_media_marker`, `pin_error_type`, `reason_max_chars`, `marker_path_factory`, `label`
    - `section_name` replaces the two hardcoded `"staticImagePin"` uses
    - _Requirements: 8.6, 8.7_
  - [x] 5.3 `camera_sync/agent.py`
    - A `video_pin_worker` (section `staticVideoPin`, video store, marker `static_video_camera/applied_pin_request.json`, cap 100 MB, marker "no video is pinned", reason truncated to 256 chars), started and stopped with the agent
    - `on_delta` routes `state.staticVideoPin`; the startup catch-up hands off `desired.staticVideoPin`
    - Discovery-managed rejection includes the video id
    - `_load_inventory` passes the video pin state with its own absence seeding (marker or shadow)
    - `MAX_REPORT_BYTES = 4608` (design Decision 7; 5 KB overruns the limit in the measured worst case)
    - _Requirements: 4.6, 4.7, 8.6, 8.7, 10.5_
  - [x] 5.4 `workflow_engine/runtime.py`: the `inventory_provider` passes the video pin state, guarded like the image state
    - _Requirements: 4.4_
  - [x] 5.5 `workflow_engine/camera_binding.py`: `_CAMERA_ID_CAPABILITY_FAMILIES = ("staticImage", "staticVideo")`
    - _Requirements: 4.4, 4.8_
  - [x]* 5.6 Write property test for enumeration and inventory independence
    - **Property 10: Enumeration and inventory iff pinned, independent of the image camera**
    - **Validates: Requirements 2.1, 2.2, 2.4, 2.5, 4.6, 4.7, 6.1, 6.2**
    - `test_property_video_enumeration_independence.py`
  - [x]* 5.7 Write example tests for the sync paths
    - Agent routing: `staticVideoPin` goes to the video worker and `staticImagePin` still goes to the image worker, including the startup catch-up for both
    - Video worker end-to-end with the fake S3 and shadow: applied with metadata; failed with the store reason (AV1); remove on empty is an applied no-op; marker idempotence; reason truncation
    - Runtime inventory provider with a video pinned
    - Binding resolution of a `staticVideo` capabilities entry
    - Report-size headroom: a report at the 4.5 KB cap plus both slots at their bounds stays ≤ 8 KB
    - The existing headroom test's bound is updated consciously
    - _Requirements: 4.4, 4.6, 4.7, 8.6, 8.7, 10.5_

- [x] 6. Checkpoint: device suites green
  - In the arm64 CPU flask-app container, run `static_video_camera` (new) plus the existing `static_image_camera`, `camera_sync`, `workflow_engine` and security preservation suites; the existing tests pass unmodified apart from the listed conscious updates
  - Ask the user if questions arise

- [x] 7. Portal backend
  - [x] 7.1 Vendor `edge-cv-portal/backend/functions/video_loop.py` as a byte-identical copy of the device module
    - _Requirements: 8.2_
  - [x]* 7.2 Write property test for sniff parity and vendored-copy identity
    - **Property 11: Sniff parity (device ⇔ Portal) and totality**
    - **Validates: Requirements 1.3, 8.2**
    - `edge-cv-portal/backend/tests/test_video_loop_parity_properties.py`
    - Loads the device module by path; uses the shared vectors and random bytes; asserts equal sha256 for the two files
  - [x] 7.3 `functions/pin_requests.py`: an `sk_prefix` keyword (image default) on the item builder, queries, get, supersede, confirmation and status view; `validated_metadata` stored and shown as `latest.validatedMetadata`
    - _Requirements: 8.5, 8.8, 8.9_
  - [x] 7.4 `functions/camera_registry.py`
    - Parallel video handlers for `…/static-video/upload-url`, `…/static-video/pin` (POST, DELETE) and `…/static-video` (GET). The image handlers are untouched (a `PinSlot` refactor of them was rejected)
    - `validate_pin_video`: download to `/tmp`, then run `probe_video` in a child process with a timeout and parse its JSON; always clean up
    - Audit actions `pin_static_video` / `remove_static_video`
    - New entry point `functions/camera_video_pin.py`, serving only the static-video routes
    - _Requirements: 8.1–8.5, 8.9_
  - [x] 7.5 `functions/camera_sync.py`: route `reported.staticVideoPin` to the video family, with applied-remove convergence on `CAMERA#static-video-camera` (an omitted video entry is marked absent, never deleted)
    - _Requirements: 8.7, 8.8, 4.7_
  - [x] 7.6 `functions/deployments.py`: add `StaticVideo` to the `aravis_camera_source` compatible types
    - _Requirements: 4.8_
  - [x]* 7.7 Write property test for Portal video submission acceptance and isolation
    - **Property 12: Portal video submission acceptance and isolation**
    - **Validates: Requirements 8.2, 8.3, 8.5, 8.9**
    - `test_pin_video_submission_properties.py` on the existing `pin_env` fixtures
    - The probe runs in-process in tests, through an injectable runner; the child-process runner is covered by 7.8
  - [x]* 7.8 Write example tests for the Portal video paths (`test_pin_video_routes_examples.py`)
    - Authorization and unknown device
    - The timeout message (stub child), and the child crash
    - Status view isolation from image requests
    - Ingest routing and applied-remove convergence
    - The deployment compatible set
    - An end-to-end emulated-shadow run of pin → device echo → applied
    - _Requirements: 8.1–8.9, 4.8_
  - [x] 7.9 Run the full Portal backend suite in the py3.12 image; the existing pin suites pass unmodified
    - _Requirements: 6.3_

- [x] 8. Portal infrastructure
  - [x] 8.1 `infrastructure/lib/compute-stack.ts`
    - `VideoLayer`: bundling of `opencv-python-headless==4.11.0.86` and `numpy==1.26.4` (manylinux x86_64, cp312) with `cv2/data` removed, by `backend/layers/video/build.sh` (a local `tryBundle` with a per-requirements wheel cache) and a Docker fallback
    - `CameraVideoPinHandler`: python3.12, handler `camera_video_pin.handler`, 2048 MB, 30 s, 1 GiB ephemeral storage, role `cameraRegistryHandler.role`, layers shared + video, `COMPONENT_BUCKET` env
    - _Requirements: 8.1, 8.2, 8.4_
  - [x] 8.2 `infrastructure/lib/camera-registry-api-stack.ts`: four routes on `/devices/{id}/cameras/static-video`, `…/upload-url`, `…/pin` (POST, DELETE), integrated with the new function under the Cognito authorizer
    - _Requirements: 8.1_
  - [x]* 8.3 Update and extend the infra jest tests (`camera-registry-infra.test.ts`)
    - The new function's configuration and role reuse (no new `AWS::IAM::Role` or policy)
    - VideoLayer attached only to the new function
    - Bundled layer size ≤ 200 MB
    - The new routes carry the authorizer
    - The route-count assertion is updated consciously
    - _Requirements: 8.1_
  - [x] 8.4 Run `tsc`, the full infra jest suite and the security IAM synth gate (`test_preservation_iam_cdk_synth.py`); the gate passes without a baseline change
    - _Requirements: 8.1_

- [x] 9. Portal frontend
  - [x] 9.1 Upload progress: reuse `putFileWithProgress` (`utils/detectorConversion.ts`, an XHR PUT with progress callbacks); no new helper
    - _Requirements: 9.4_
  - [x] 9.2 `services/api.ts` (four video methods) and `pages/workflows/cameraReference.ts`
    - Video status and metadata types, and `STATIC_VIDEO_FOCUS_VALUE`
    - `StaticVideo` in `isAravisCompatibleCamera()`
    - The `capabilities.staticVideo.id` fallback in `cameraIdValue()`
    - _Requirements: 4.8, 9.5, 9.6_
  - [x] 9.3 `components/StaticVideoPanel.tsx`, mounted after `StaticImagePanel` in `DeviceCamerasTab.tsx` with `?focus=static-video` support; `DeviceDetail.tsx` reads the focus value
    - Status, failure reason, device-reported presence, and the connectivity hint while pending
    - The validating state and a rejected validation's message (task 11)
    - Upload with the 100 MB pre-check and a progress bar
    - Video metadata rows and the loop note
    - Replace/remove gated on the mutation role
    - _Requirements: 8.2–8.4, 9.1–9.6_
  - [x] 9.4 `NodeConfigPanel.tsx`: a second shortcut, "Pin a test video…" (`data-testid="pin-static-video-shortcut"`) with the video focus value
    - _Requirements: 9.7_
  - [x]* 9.5 Write and update the vitest suites
    - New `StaticVideoPanel.test.tsx`, `DeviceCamerasTab.staticVideo.test.tsx`, `NodeConfigPanel.videoShortcut.test.tsx`, `DeviceDetail.staticVideoFocus.test.tsx` and `staticVideoCameraReference.test.ts`
    - Conscious updates where a test enumerates the compatible types (`aravisCameraReference.property.test.ts`, `staticImageCameraPreservation.property.test.ts`), each commented with the requirement
    - Cloudscape test-utils lookups stay out of `waitFor` callbacks: one that finds nothing keeps re-triggering waitFor's MutationObserver, so a failing check hangs instead of failing
    - _Requirements: 4.8, 9.1–9.7_
  - [x] 9.6 Run the frontend vitest suite and `npm run build`
    - _Requirements: 9.1_

- [x] 10. Report cap from the account quota (design Decision 7)
  - [x] 10.1 Device: `camera_sync/agent.py` derives the cap from the shadow size limit (`report_cap_for_shadow_limit`: limit − 3,584, within 1,024..10,240), reads ShadowManager's `shadowDocumentSizeLimitBytes` over IPC every 300 s (`shadow_manager_size_limit_provider`), and halves the cap after a size rejection; `utils/server_setup.py` wires the provider
    - _Requirements: 10.5, 10.6_
  - [x] 10.2 Portal: `functions/deployments.py` reads the IoT quota `L-A295A064` (`account_shadow_document_limit`, cached 15 minutes) and writes min(quota, 30,720) into the ShadowManager merge (`apply_shadow_document_size_limit`) on LocalServer deployments and on workflow revisions carrying ShadowManager
    - _Requirements: 10.6_
  - [x] 10.3 IAM: `servicequotas:GetServiceQuota` on the one quota ARN for the Deployments role and `DDAPortalAccessRole`, recorded in `iam_post_fix_approved_additions.json`; the IAM synth gate passes
    - _Requirements: 10.6_
  - [x]* 10.4 Tests
    - Device: `static_video_camera/test_video_report_cap.py` (cap table and property, provider parsing, refresh, back-off including the real IPC error shape, headroom at 8–30 KB limits) and the `server_setup` wiring test in `camera_sync/test_server_setup_isolation.py`
    - Portal: `tests/test_shadow_document_size_limit.py` (quota read and cache, merge rules and property, both call sites)
    - Infra: both grants in `camera-registry-infra.test.ts`
    - _Requirements: 10.5, 10.6_

- [x] 11. Asynchronous Portal validation with a 60 s budget (design Decision 8, Requirements 8.2–8.5)
  - [x] 11.1 `POST …/static-video/pin`: the synchronous checks (authorization, body, 100 MB, staged object present), superseding submissions still validating, then a `VIDEO_VALIDATION#` record in state `validating`, an asynchronous self-invocation (`dispatch_video_validation`), and a 202 response; a failed invocation rejects the record (502). A removal also supersedes validating submissions
  - [x] 11.2 The validation job (`run_video_validation`, routed by `camera_video_pin.handler`): ignore a record no longer validating; refuse one older than 5 minutes; supersede when a newer validation or video pin request exists (before the download and after the decode); download and probe with a 60 s timeout; on rejection record the message and delete the staged object; on acceptance run the submission flow (`submit_validated_video`) and record the pin request id
  - [x] 11.3 `pin_requests.py` record helpers and `video_validation_view`: the video status view reports the latest validation; a `validating` record older than 5 minutes shows as expired
  - [x] 11.4 Infra: the fixed function name `dda-portal-camera-video-pin`, a 120 s timeout, `VIDEO_VALIDATION_FUNCTION`, async retries off (event age ≤ 5 minutes), and `lambda:InvokeFunction` on the function itself, recorded as an approved addition
  - [x]* 11.5 Tests: Property 12 and the example tests for the asynchronous flow (`test_pin_video_submission_properties.py`, `test_pin_video_routes_examples.py`, with the inline and queued dispatch seams in `video_pin_helpers.py`), the infra jest assertions, and the IAM gate

- [x] 12. Checkpoint: all local suites green
  - Device suites, the Portal backend suite, infra jest and the IAM gate, frontend vitest and build
  - Ask the user if questions arise

- [x] 13. Cross-platform container verification
  - [x] 13.1 Run the preservation guard suites and the full security preservation suite in the flask-app container; only the two intended `iam_out_of_scope_baseline.json` hashes changed
    - _Requirements: 10.2_
  - [x] 13.2 Run the `static_video_camera`, `static_image_camera` and `camera_sync` suites in each platform image
    - This host: arm64 CPU, `arm64JP5`, `arm64JP6`, `arm64JP7`
    - x86 build server: amd64 and `amd64Nvidia`
    - Record each image's `cv2` build line and pass counts in `verification-notes.md`
    - _Requirements: 10.1_

- [x] 14. Build, deploy, and verify on devices
  - [x] 14.1 Pre-build checks per `.kiro/steering/builds.md`: no build running, preservation guards green, `cdk.out` moved aside, no Portal deploy in flight
  - [x] 14.2 Build and publish the LocalServer components
    - One build at a time per source tree; separate build servers may run in parallel
    - JP5 on this host, JP6 on the JP6 build server, JP7 on the JP7 build server, amd64 and amd64Nvidia on the x86 build server
    - Published: JP5 1.0.47, JP6 1.0.70, JP7 1.0.47, amd64 1.0.42. amd64Nvidia was skipped by agreement because there is no test device.
  - [x] 14.3 After the builds finish, deploy the Portal (`deploy-infrastructure.sh`, then `deploy-frontend.sh`); verify the new routes return 401 unauthenticated, then exercise them with a temporary Cognito user and delete it afterwards
  - [x] 14.4 On-device end-to-end on MIC-730 (JP5), Orin AGX (JP6), thor1 (JP7) and Dell (amd64)
    - Done on all four devices, each with a 30-minute soak. The MIC-730 and the Dell have no Portal use case, so their pins went through the device API.
    - Criterion 6.4's same-workflow clause is deferred to the `multi-source-workflows` spec.
    - Finding: after a device-API replace, the inventory entry kept the previous video's metadata until the next camera-registry report. Fixed and re-verified in 14.6.
    - Deploy the new component
    - Pin an image and a video from the Portal: both applied, and both cameras shown in the registry and the picker
    - The video preview advances, and captures seconds apart differ
    - A workflow with one node per camera runs repeatedly on changing video frames
    - Replace the video, remove it (the image camera is unaffected), then remove the image
    - The deployed ShadowManager limit and the agent's report cap match the account quota
    - The backend stays healthy for 30 minutes or more
    - Record per-device grab latency
    - _Requirements: 2.1–2.6, 3.1–3.10, 4.1–4.9, 5.1–5.5, 6.1–6.4, 7.1–7.2, 8.1–8.9, 10.1, 10.5, 10.6_
  - [x] 14.5 Write `verification-notes.md`: what was verified on which device and image, timings, and deviations
  - [x] 14.6 Fix the inventory lag after a device-API pin change (the 14.4 finding; the user chose to fix it now)
    - `endpoints/static_video_camera.py` calls `camera_sync.hooks.notify_image_source_changed()` after every successful pin, replace, pin by reference and unpin. Rejected requests do not call it. The Static_Image_Camera endpoint is unchanged (Requirement 6.3)
    - Tests in `test_video_endpoints.py`: each successful change requests exactly one report; rejected requests request none; a failing agent does not fail the pin
    - Suites: `static_video_camera` 130 passed; `static_image_camera` 125 passed and 1 failed, the known RLock `.locked()` failure; `camera_sync` 94 passed; `security` 233 passed and 15 skipped; guard pair 4 passed and 3 skipped; IAM out-of-scope guard 2 passed. No baseline changes
    - Rebuilt and published every variant, one build at a time: JP5 1.0.48, amd64 1.0.43, JP6 1.0.71, JP7 1.0.48
    - Redeployed the four devices
    - On each device, a device-API pin, replace and unpin reached the camera-registry shadow in 1.8–5.2 s. In each 30-minute soak, 30 of 30 replaces did too, with no health failures and no restarts. See `verification-notes.md` 14.6
    - _Requirements: 4.6_

- [ ] 15. Commit and push (with the user's go-ahead)
  - Commit on `spec/static-camera-video-loop`, stating the device verification and the two rebaselined hashes; merge or push to `integration/all-specs` as the user directs
