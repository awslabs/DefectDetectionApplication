# Implementation Plan: static-image-camera-source

## Overview

Python implementation following existing backend patterns, in dependency order:

1. **Core store** — the gi-free `StaticImageStore` module (`src/backend/utils/static_image_camera.py`) that owns pinning, validation, atomic persistence under `COMPONENT_WORK_PATH/static_image_camera/`, and RGB frame synthesis. Most of the 12 correctness properties exercise this module directly over `tmp_path` directories.
2. **Pin_API** — new FastAPI router `src/backend/endpoints/static_image_camera.py` (POST/GET/DELETE `/static-image-camera/pin`) registered in `app.py`.
3. **Provider integrations** — the only two existing modules touched: `edge_ml1_p_camera_management/aravis_functions.py` (synthetic enumeration entry) and `utils/camera_manager.py` (short-circuits for the fixed id `static-image-camera`).
4. **Wiring integration tests** — executor feed planning and Image_Source/preview/capture paths, all mock-based (no consumer code changes to verify, only that the static id flows through unchanged plumbing).
5. **Container verification** — the new suite in the flask-app container (x86, no cameras — this run is the Cloud_Environment criterion).
6. **On-device verification** — manual, per the workspace `builds.md` rule; cannot be automated from this environment.

Property-based tests implement the design's 12 correctness properties with Hypothesis, minimum 100 examples each (`HYPOTHESIS_PROFILE=ci`), one test per property in its own module under `test/backend-test/static_image_camera/`, tagged `**Feature: static-image-camera-source, Property {N}: {property_text}**`. Images are generated in memory with Pillow (random dimensions, random pixels, format drawn from JPEG/PNG/BMP); the size-limit branch uses an injected small `max_file_bytes` so no test needs large files, hardware, or `gi`. No preservation-tracked file (docker-compose, Dockerfiles, requirements.txt, recipes) changes — no security baselines to rebaseline.

## Tasks

- [x] 1. Core static image store module
  - [x] 1.1 Create `src/backend/utils/static_image_camera.py`
    - Module-level constants: `STATIC_IMAGE_CAMERA_ID = "static-image-camera"` (never derived from image content), `STATIC_IMAGE_CAMERA_IDENTITY` dict with all seven non-empty identity fields (id, model, address, physical_id, protocol, serial, vendor) per the design, `MAX_PIN_FILE_BYTES = 50 * 1024 * 1024`, `SUPPORTED_FORMATS = ("JPEG", "PNG", "BMP")`
    - Exceptions `StaticImagePinError` and `StaticImageUnavailableError`
    - `StaticImageStore(base_dir=None, max_file_bytes=MAX_PIN_FILE_BYTES)` defaulting `base_dir` to `$COMPONENT_WORK_PATH/static_image_camera`; disk is the source of truth (every public method re-reads metadata; `(mtime, size)`-keyed decode cache)
    - `pin_bytes(data, file_name)`: size check, Pillow decode + format check against `SUPPORTED_FORMATS`, `ImageOps.exif_transpose`, all validation before any replace; write original bytes to a `.tmp-*` staging file then `os.replace` onto `pinned_image`, then `os.replace` the `pinned_image.json` metadata sidecar (fileName, format, width, height post-transpose, fileSizeBytes, pinnedAtEpochMs); invalidate the decode cache; raise `StaticImagePinError` leaving prior state untouched on any failure
    - `pin_file(path, captures_root)`: realpath + commonpath guard rejecting anything outside `captures_root`, not-found error for missing files, then `pin_bytes` validation
    - `status()` → `{'pinned', 'cameraId', 'metadata'}`, `is_pinned()`, `unpin()` (delete image + sidecar; `StaticImagePinError` when nothing pinned), distinguishing missing data (not pinned, no error) from undecodable data (log error identifying the cause, report not pinned)
    - `get_frame()` → `{'data': packed 24-bit RGB bytes, 'width', 'height', 'pixel_format': 'RGB'}` with `len(data) == 3 * width * height`; snapshot under the store lock; raise `StaticImageUnavailableError` naming `static-image-camera` and "no usable pinned image" when no pin exists or the file cannot be read/decoded
    - `get_store()` module-level singleton
    - No `gi` imports anywhere in the module
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 2.4, 3.1, 3.2, 3.3, 3.5, 3.7, 5.1, 5.3, 5.4, 5.5, 6.1, 6.2, 6.3, 6.4, 6.5_

  - [x]* 1.2 Write property test for pin round-trip fidelity
    - **Property 1: Pin round-trip fidelity**
    - **Validates: Requirements 1.1, 1.5, 1.6, 3.1, 3.2, 3.7**
    - New module `test/backend-test/static_image_camera/test_property_pin_round_trip.py`; Hypothesis over valid images (any dimensions, pixel content, format in {JPEG, PNG, BMP}) and prior pin states; metadata reports decoded width/height/format/file name; grab `data` equals the packed RGB decode byte for byte with `pixel_format == "RGB"` and `len(data) == 3 * width * height`

  - [x]* 1.3 Write property test for invalid pin input preserving prior state
    - **Property 6: Invalid pin input preserves prior state**
    - **Validates: Requirements 1.3, 1.4, 1.8, 5.3**
    - New module `test/backend-test/static_image_camera/test_property_invalid_pin.py`; Hypothesis over prior states (valid pin or none) and invalid inputs (undecodable bytes, oversized via injected `max_file_bytes`, nonexistent capture reference); descriptive error (formats list / size limit / not-found) and afterwards status, metadata, frame content, and enumeration inclusion identical to prior state

  - [x]* 1.4 Write property test for pin-from-capture parity
    - **Property 7: Pin-from-capture parity**
    - **Validates: Requirements 1.7**
    - New module `test/backend-test/static_image_camera/test_property_pin_from_capture.py`; Hypothesis over valid images written under a tmp captures root: pin-by-reference yields metadata and frame equal to pinning the same bytes directly; paths resolving outside the captures root are rejected without state change

  - [x]* 1.5 Write property test for replace atomicity and freshness
    - **Property 8: Replace atomicity and freshness**
    - **Validates: Requirements 5.1, 5.2**
    - New module `test/backend-test/static_image_camera/test_property_replace_atomicity.py`; Hypothesis over pairs of valid images A and B: grabs after the second pin's confirmation return exactly B's decoded content; every grab at any point equals exactly one of the two decodes in its entirety, never a mix

  - [x]* 1.6 Write property test for the unpin lifecycle
    - **Property 9: Unpin lifecycle**
    - **Validates: Requirements 5.4, 5.5**
    - New module `test/backend-test/static_image_camera/test_property_unpin_lifecycle.py`; Hypothesis over pinned states: removal succeeds, status reports no pin, enumeration excludes the camera, grabs fail; over unpinned states: removal fails with "no image is pinned" and changes neither stored state nor enumeration

  - [x]* 1.7 Write property test for no-usable-image grab failure
    - **Property 10: No-usable-image grab failure**
    - **Validates: Requirements 3.5**
    - New module `test/backend-test/static_image_camera/test_property_grab_failure.py`; Hypothesis over unusable states (never pinned, unpinned, pinned file deleted out from under the store, stored bytes corrupted): grab raises an error whose message names `static-image-camera` and indicates no usable pinned image is available

  - [x]* 1.8 Write property test for restart persistence round trip
    - **Property 11: Restart persistence round trip**
    - **Validates: Requirements 6.1, 6.2, 6.3**
    - New module `test/backend-test/static_image_camera/test_property_restart_persistence.py`; Hypothesis over pinned images: a fresh `StaticImageStore` over the same directory reports pinned on its first status call with metadata equal to pre-restart metadata, and its first grab is byte-for-byte identical to a pre-restart grab

  - [x]* 1.9 Write property test for corruption containment at restore
    - **Property 12: Corruption containment at restore**
    - **Validates: Requirements 6.4, 6.5**
    - New module `test/backend-test/static_image_camera/test_property_corruption_containment.py`; Hypothesis over corruption modes (image file missing, metadata sidecar missing, undecodable image bytes): a fresh store completes construction, logs an error identifying the cause category (missing versus undecodable), reports no pin, leaves enumeration returning exactly the physical cameras, and accepts a subsequent valid pin restoring normal behavior

- [x] 2. Checkpoint - Ensure all tests pass
  - Run the new store tests via the backend suite's standard pytest invocation; ensure all tests pass, ask the user if questions arise.

- [x] 3. Pin_API endpoints
  - [x] 3.1 Create `src/backend/endpoints/static_image_camera.py`
    - FastAPI router via `get_api_router()` following the `endpoints/camera.py` pattern
    - `POST /static-image-camera/pin` multipart `file` variant: read the body with a hard cap (reject as soon as more than `MAX_PIN_FILE_BYTES` are consumed, never buffering unbounded input), delegate to `pin_bytes`; 200 → `{cameraId, metadata}`; 400 listing JPEG/PNG/BMP on undecodable input; 400 naming the 50 MB limit on oversize
    - `POST /static-image-camera/pin` JSON `{"capturedImagePath": ...}` variant: delegate to `pin_file` with the captures root under `COMPONENT_WORK_PATH`; 400 not-found for missing references; 400 for paths escaping the captures root
    - `GET /static-image-camera/pin`: pin status + metadata from `store.status()`
    - `DELETE /static-image-camera/pin`: unpin; 400 "no image is pinned" when nothing pinned
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 5.1, 5.3, 5.4, 5.5_

  - [x] 3.2 Register the Pin_API router in `src/backend/app.py`
    - Add the router registration alongside the existing endpoint routers (same include pattern as `endpoints/camera.py`)
    - _Requirements: 1.1, 1.6_

  - [x]* 3.3 Write endpoint tests for the Pin_API
    - New module `test/backend-test/static_image_camera/test_endpoints.py` using FastAPI `TestClient`
    - Happy paths: pin upload → `{cameraId, metadata}`, status with and without a pin, unpin, replace returning success confirmation, pin-from-capture
    - Error paths: undecodable upload (400 naming JPEG/PNG/BMP), oversize upload (400 naming the limit, via injected small limit), missing capture reference (400 not-found), path escaping the captures root (400), unpin with nothing pinned (400 "no image is pinned")
    - _Requirements: 1.1, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8, 5.4, 5.5_

- [x] 4. Camera enumeration integration
  - [x] 4.1 Modify `src/backend/edge_ml1_p_camera_management/aravis_functions.py`
    - `getCameras()`: after the existing physical-camera loop, inside `try/except`: `if get_store().is_pinned(): cameras.append(Camera(**STATIC_IMAGE_CAMERA_IDENTITY))`; on any store exception, log and return the physical list unchanged; `rescan_cameras()` inherits via its existing `getCameras()` call — no change there
    - `getCamera(cameraId)`: for `STATIC_IMAGE_CAMERA_ID`, return a truthy sentinel when pinned (the connect endpoint uses this purely as an existence check) and raise `AravisCameraNotFound` mentioning the pin requirement when not pinned; physical ids take the existing path untouched
    - _Requirements: 1.2, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6, 2.7_

  - [x]* 4.2 Write property test for enumeration inclusion iff pinned
    - **Property 2: Enumeration includes the static camera iff pinned, preserving physical cameras**
    - **Validates: Requirements 1.2, 2.1, 2.2, 2.3, 2.5, 2.6, 7.1, 7.3**
    - New module `test/backend-test/static_image_camera/test_property_enumeration.py` using the existing `mock_gi.py` pattern; Hypothesis over physical camera lists (including empty, modeling Cloud_Environment) and pin/replace/unpin sequences: exactly one static entry iff pinned, all seven identity fields non-empty, physical entries exactly the input list unchanged and in order

  - [x]* 4.3 Write property test for fixed identifier invariance
    - **Property 3: Fixed identifier invariance**
    - **Validates: Requirements 2.4**
    - New module `test/backend-test/static_image_camera/test_property_identifier.py`; Hypothesis over pin/replace/restart (fresh store over the same directory) sequences and generated physical identifier sets: the static identifier is character-for-character identical after every operation and never equals any physical identifier

  - [x]* 4.4 Write property test for enumeration resilience
    - **Property 4: Enumeration resilience to static-entry failure**
    - **Validates: Requirements 2.7**
    - New module `test/backend-test/static_image_camera/test_property_enumeration_resilience.py`; Hypothesis over physical camera lists with the static-entry construction forced to raise arbitrary exceptions: the result equals exactly the physical list and no exception propagates

- [x] 5. Camera manager short-circuit integration
  - [x] 5.1 Modify `src/backend/utils/camera_manager.py`
    - `get_camera_frame(id, config)`: check `id == STATIC_IMAGE_CAMERA_ID` before touching `get_frame_lock`, `camera_objects`, or `connect_camera`; return `get_store().get_frame()`; wrap `StaticImageUnavailableError` in the existing `Exception` contract with a message naming the static camera; `config` (gain/exposure/advancedSettings) accepted and ignored
    - `connect_camera(id)`: `True` when pinned, `AravisCameraException` naming the camera when not; never construct a `manager_base.Camera`
    - `disconnect_camera(id)`: no-op `True`
    - `get_camera_status(id)`: `CONNECTED` when pinned, `DISCONNECTED` otherwise
    - `get_camera_feature_bounds(id)`: explicit `{}` (never connect on demand)
    - `apply_camera_features(id, features)`: return `{}` without connecting
    - All physical-id paths byte-identical to today
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7, 3.8, 5.7, 7.2, 7.4_

  - [x]* 5.2 Write property test for grab determinism and config invariance
    - **Property 5: Grab determinism and acquisition-config invariance**
    - **Validates: Requirements 3.3, 3.4**
    - New module `test/backend-test/static_image_camera/test_property_grab_determinism.py` using the `mock_gi.py` pattern; Hypothesis over pinned images, repeat counts, and arbitrary acquisition config dicts (gain, exposure, advancedSettings, or none): every `get_camera_frame` completes without error and returns byte-for-byte identical `(data, width, height, pixel_format)` regardless of config

  - [x]* 5.3 Write unit tests for the short-circuit wiring
    - New module `test/backend-test/static_image_camera/test_camera_manager_short_circuit.py` (mock-based, `mock_gi.py` pattern)
    - Static grab succeeds without touching `camera_objects` or calling `connect_camera` (Req 3.6); pin/replace/unpin operations perform zero camera-manager interactions and disturb no mocked physical camera object (Req 5.7)
    - `connect_camera`/`get_camera_status`/`get_camera_feature_bounds`/`apply_camera_features` static-id behaviors, pinned and unpinned, including `AravisCameraException` naming the camera when unpinned
    - _Requirements: 3.6, 5.7_

- [x] 6. Checkpoint - Ensure all tests pass
  - Run the full new `test/backend-test/static_image_camera/` suite plus the existing camera-manager and enumeration test modules via the standard pytest invocation; ensure all tests pass, ask the user if questions arise.

- [x] 7. Consumer wiring integration tests
  - [x]* 7.1 Write executor feed-planning and failure-path tests
    - New module `test/backend-test/static_image_camera/test_workflow_feed.py`
    - `plan_aravis_feeds` with a binding resolved to `static-image-camera` yields the expected `AravisFeed(camera_id="static-image-camera")` with no workflow document modification and no node catalog addition (Req 4.2, 4.5)
    - Executor run (mocked `run_pipeline`) whose static grab raises fails with `failing_node_id` set on the Aravis node and the error naming the Static_Image_Camera, leaving other runs unaffected (Req 5.6)
    - _Requirements: 4.2, 4.5, 5.6_

  - [x]* 7.2 Write Image_Source, preview, and capture wiring tests
    - Extend `test/backend-test/static_image_camera/test_endpoints.py` or new module `test/backend-test/static_image_camera/test_image_source_wiring.py` (gst executor mocked)
    - Image_Source CRUD accepts the static `cameraId` with the same ImageSourceConfiguration fields as physical cameras and persists a retrievable record (Req 4.1)
    - Preview and capture endpoints serve/store Pinned_Image content through the existing paths (Req 4.3, 4.4)
    - Preview/capture with no pin fail with a descriptive error naming the Static_Image_Camera and store no captured image (Req 4.6)
    - _Requirements: 4.1, 4.3, 4.4, 4.6_

- [x] 8. Container verification (Cloud_Environment parity)
  - [x] 8.1 Run the new suite in the flask-app container
    - Execute exactly (x86, no cameras — this run is the Cloud_Environment criterion of Requirement 7):
      ```
      docker run --rm -v "$(pwd)":/repo -w /repo \
        -e PYTHONPATH=/repo/src/backend:/repo/test/backend-test \
        flask-app:latest bash -lc \
        'PY=$(command -v python3.11 || command -v python3.10); \
         $PY -m pip install --no-cache-dir --quiet pytest sarge testfixtures hypothesis; \
         $PY -m pytest test/backend-test/static_image_camera -q -p no:cacheprovider'
      ```
    - All tests must pass; fix any failures before proceeding
    - _Requirements: 7.1, 7.2_

- [ ] 9. On-device verification (manual — workspace builds rule, before commit)
  - [ ]* 9.1 Verify the static image camera end-to-end on real hardware
    - Manual: cannot be fully automated from this environment. Per the workspace `builds.md` rule, this on-device edge feature must be verified on real hardware on every architecture it ships to before commit
    - Build and deploy (or hot-patch for iteration) the component to a Jetson device (JP5 and/or JP6, plus JP7 where applicable) and to an x86 cloud instance
    - Exercise end to end: pin an image via the Pin_API → `GET /cameras` shows the static entry (alongside a physical camera on the Jetson, Req 7.3) → configure an Image_Source → live preview → capture → run a workflow whose `aravis_camera_source` binds to `static-image-camera` → replace the image → unpin → confirm the workflow error path
    - Confirm parity (Req 7.5): same identifier, identity fields, and matching frame checksum for the same image across environments
    - Confirm the backend stays healthy for a sustained period (no crash, no container restart, no crash-loop) and that a concurrently previewing physical camera is undisturbed (Req 7.4)
    - State in the commit/PR what was verified on which device(s)
    - _Requirements: 7.3, 7.4, 7.5_

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP, except that the design's Testing Strategy treats the property suite as the primary evidence for the store's correctness — skipping them weakens the container verification in task 8
- Each task references specific requirements for traceability
- Property tests use Hypothesis, minimum 100 examples each (`HYPOTHESIS_PROFILE=ci`), one property per module, tagged `**Feature: static-image-camera-source, Property {N}: {property_text}**`
- `src/backend/utils/static_image_camera.py` must stay free of `gi` imports so the property suite runs on any host; integration tests of `aravis_functions` and `camera_manager` use the existing `mock_gi.py` pattern
- No preservation-tracked files change (docker-compose, Dockerfiles, `requirements.txt`, recipes — Pillow and numpy are already backend dependencies), so no security-preservation baselines need rebaselining
- Requirement 3.8 (10-second grab bound) is satisfied by construction (in-memory/disk read) and observed during on-device verification rather than asserted in a flaky unit-level timer
- Task 9.1 is manual and marked optional only because it cannot be executed by a coding agent from this environment; per the workspace `builds.md` rule it is mandatory before commit

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1"] },
    { "id": 1, "tasks": ["1.2", "1.3", "1.4", "1.5", "1.6", "1.7", "1.8", "1.9", "3.1", "4.1", "5.1"] },
    { "id": 2, "tasks": ["3.2", "4.2", "4.3", "4.4", "5.2", "5.3"] },
    { "id": 3, "tasks": ["3.3", "7.1", "7.2"] },
    { "id": 4, "tasks": ["8.1"] },
    { "id": 5, "tasks": ["9.1"] }
  ]
}
```
