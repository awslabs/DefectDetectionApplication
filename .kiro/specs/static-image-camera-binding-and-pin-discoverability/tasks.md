# Implementation Plan

## Overview

This plan fixes three defects using the exploratory bugfix workflow — reproduce first, capture
existing behavior, apply the minimal fix, then validate — across TWO independently shippable
tracks:

- **Portal track (tasks 1-5, frontend only)**: the Static_Image_Camera binding dead end (Defect 1)
  and the pin-panel discoverability dead end (Defect 2). Ships in minutes through
  `./deploy-frontend.sh` and does NOT depend on the device track.
- **Device track (tasks 6-10, LocalServer component)**: the duplicate Static_Image_Camera
  registration in the Camera_Registry (Defect 3). Needs a Greengrass component build (~100 minutes,
  user-driven) plus a deployment revision, so it ends at "ready for the user's build" and does NOT
  block the portal deploy.

Defect 1 is fixed at the id-resolution point: `cameraIdValue()` in
`edge-cv-portal/frontend/src/pages/workflows/cameraReference.ts` falls back to the StaticImage
capabilities identity (`capabilities.staticImage.id`) when `params.cameraId` is absent, so
`applyAravisCameraSelection()` populates `camera_id` for the entry the picker already offers.
The device-side inventory shape (`src/backend/camera_sync/inventory.py`: `params={}` plus
`capabilities={"staticImage": …}`) is NOT changed — it is the shipped, hardware-verified contract
the named shadow, the absence reporting, and the Portal's discovery-managed mutation rejection all
depend on, and changing it would require a device component rebuild and re-sync.
`isAravisCompatibleCamera()` is NOT changed either; its acceptance of `StaticImage` is correct.

Defect 2 is fixed by carrying a focus flag from the node panel's shortcut through
`DeviceDetail.tsx` into `DeviceCamerasTab.tsx`, which scrolls the "Static image camera" panel into
view and flags it on arrival. `StaticImage` is NOT added to the create-form type options — the
backend rejects manual creation of discovery-managed sources.

Defect 3 is fixed in the inventory merge, NOT in the enumeration: `build_inventory()` excludes the
aravis-enumerated static camera (a discovered Aravis camera whose `camera_id` equals
`STATIC_IMAGE_CAMERA_ID`) from the reported discovery entries, because it already appends its own
richer dedicated entry carrying the pin metadata and the explicit-absence lifecycle.
`getCameras()` in `src/backend/edge_ml1_p_camera_management/aravis_functions.py` KEEPS appending the
static camera — the device's `/cameras` route, `rescan_cameras()`, the `getCamera()` short-circuit,
Image_Source creation, the camera-manager grab path, and local aravis frame feeds all depend on it
enumerating like a GenICam camera (base spec Requirements 2.1-2.3, 3.x, 4.1); removing it there
would break device-local frame serving. The cloud report is the only place that de-duplicates.
`aravis_stable_id()`, the Aravis mapping for real bus cameras, the physical-camera absence tracking,
and the Fake camera's handling are NOT changed.

Because AWS IoT shadow updates MERGE nested maps, a device that already published the duplicate
cannot converge by omission alone (bugfix.md 1.11): the `arv-6c84191b7fe6` key stays alive in the
shadow, every documents event still carries it, and the Portal's missing-from-report deletion path
never fires. The agent therefore retires the key ONCE with an explicit null — the same mechanism the
Portal already uses in `_clear_static_camera_shadow_key` — after which the existing missing-from-report
path deletes the registry entry with no Portal change at all.

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2", "6", "7"],
      "description": "Write tests against UNFIXED code. Portal track: task 1 (Property 1: Bug Condition) fails, task 2 (Property 2: Preservation) passes. Device track: task 6 (Property 3: Bug Condition) fails, task 7 (Property 4: Preservation) passes. All four are independent of each other."
    },
    {
      "wave": 2,
      "tasks": ["3", "8"],
      "description": "Implementations. Task 3 (frontend, depends on 1+2) then re-runs 1 and 2. Task 8 (device, depends on 6+7) then re-runs 6 and 7. The two are independent."
    },
    {
      "wave": 3,
      "tasks": ["4", "9"],
      "description": "Checkpoints. Task 4: full frontend suite plus a clean type-check (depends on 3). Task 9: device suite green in the flask-app x86 container, no component build (depends on 8)."
    },
    {
      "wave": 4,
      "tasks": ["5", "10"],
      "description": "Delivery. Task 5: frontend-only deploy and live verification (depends on 4). Task 10: device build/deploy hand-off to the user plus the post-build on-device verification checklist (depends on 9). Independent tracks - task 5 must not wait on the device build."
    }
  ]
}
```

- **Portal track**: tasks 1 and 2 are independent and must be completed BEFORE task 3. Task 3
  depends on 1 and 2 (sub-tasks 3.3 and 3.4 depend on 3.1 and 3.2). Task 4 depends on 3, task 5 on 4.
- **Device track**: tasks 6 and 7 are independent and must be completed BEFORE task 8. Task 8
  depends on 6 and 7 (sub-tasks 8.3 and 8.4 depend on 8.1 and 8.2). Task 9 depends on 8, task 10 on 9.
- **The two tracks never block each other.** The portal fix is shippable after task 4; the device
  fix reaches devices only after the user's component build and deployment (task 10).

## Tasks

- [x] 1. Write bug condition exploration test
  - **Property 1: Bug Condition** - StaticImage camera id resolves from capabilities and the pin panel is reachable
  - **CRITICAL**: These tests MUST FAIL on unfixed code - failure confirms both defects exist
  - **DO NOT attempt to fix the tests or the code when they fail**
  - **NOTE**: These tests encode the expected behavior - they will validate the fix when they pass after implementation
  - **GOAL**: Surface counterexamples that demonstrate the bugs exist
  - **Scoped PBT Approach**: Both defects are deterministic, so scope the properties to the shipped shapes. Defect 1: for any `StaticImage` entry carrying a non-empty `capabilities.staticImage.id` and an empty or absent `params` block, `cameraIdValue()` resolves that id and `applyAravisCameraSelection()` sets `camera_id` to it. Include the exact live counterexample from the `dda-camera-registry` named shadow for `jetson-thor1`: `{camera_source_id: 'static-image-camera', name: 'Static Image Camera', type: 'StaticImage', origin: 'edge-discovered', params: {}, capabilities: {staticImage: {id: 'static-image-camera', model: 'Static Image Camera', address: 'internal', physicalId: 'static-image-camera', protocol: 'StaticImage', serial: 'STATIC-IMAGE-0', vendor: 'AWS-DDA'}}, discovered: true, absent: true, absentSince: 1788839397466}`
  - Property test file (pure surface, `vitest` + `fast-check ^4.8.0`, both already in `edge-cv-portal/frontend/package.json`; setup at `src/test/setup.ts`, which caps global runs at 25): `src/pages/workflows/staticImageCameraBinding.property.test.ts`, following the conventions of the existing `src/pages/workflows/aravisCameraReference.property.test.ts` (generators + independent oracle, `structuredClone` purity checks)
  - Generator: a `StaticImage` entry arbitrary over a non-empty `capabilities.staticImage.id`, `params` drawn from `{}` / `null` / `undefined` / a record with no usable `cameraId`, arbitrary extra capability keys, and arbitrary prior node parameters (with and without a pre-existing `camera_id`)
  - Assert: `cameraIdValue(entry)` is non-null and equals `entry.capabilities.staticImage.id`; `applyAravisCameraSelection(prior, entry, deviceId).parameters.camera_id` equals that id; the returned hint still carries the source id, display name, and reference device id; neither input is mutated
  - Assert the concrete live counterexample resolves `'static-image-camera'` and that the resulting parameter record satisfies the node's required-parameter check (no `Required parameter 'camera_id' has no value` violation)
  - Defect 2 exploration file: `src/components/DeviceCamerasTab.staticImageFocus.test.tsx`, reusing the mock scaffolding already in `src/components/DeviceCamerasTab.test.tsx` (mocked `getStaticImagePinStatus` / `getStaticImageUploadUrl` / `pinStaticImage` / `removeStaticImagePin`, the `pinStatusResponse` fixture, a 9-camera `camerasResponse`). Assert that arriving with the focus flag set scrolls the `static-image-panel` container into view (spy on `Element.prototype.scrollIntoView`, which jsdom does not implement) and renders the arrival flag
  - Defect 2 shortcut file: `src/pages/workflows/NodeConfigPanel.pinShortcut.test.tsx`, asserting the shortcut's `window.open` URL carries the focus parameter alongside the existing `usecase_id` and `tab=cameras`
  - The assertions encode Expected Behavior 2.1 through 2.6 (the Fix Checking property in bugfix.md)
  - Run the tests on UNFIXED code: `cd edge-cv-portal/frontend && npx vitest run src/pages/workflows/staticImageCameraBinding.property.test.ts src/components/DeviceCamerasTab.staticImageFocus.test.tsx src/pages/workflows/NodeConfigPanel.pinShortcut.test.tsx`
  - **EXPECTED OUTCOME**: Tests FAIL (this is correct - it proves the bugs exist)
  - Document counterexamples found (expected: `cameraIdValue()` returns null for the live shadow entry, `applyAravisCameraSelection()` returns a parameter record with no `camera_id` key, the shortcut URL has no focus parameter, and no scroll or flag happens on arrival)
  - Mark task complete when the tests are written, run, and the failures are documented
  - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

- [x] 2. Write preservation property tests (BEFORE implementing fix)
  - **Property 2: Preservation** - Existing id resolution, selection semantics, picker compatibility, and Cameras tab behavior unchanged
  - **IMPORTANT**: Follow observation-first methodology - run the UNFIXED code first, record the actual outputs, then assert those recorded outputs
  - Observe on UNFIXED code: `cameraIdValue()` returns `'Basler-40123456'` for the `AravisDiscovered` fixture and `'cam-1'` for the configured `Camera` fixture; returns null for an entry with no params, with `params.cameraId: ''`, and with `params.cameraId: 42`. Record these
  - Observe on UNFIXED code: `applyAravisCameraSelection()` retains a pre-existing `camera_id` when the source resolves none, copies `gain` / `exposure` only when the source's params carry them as numbers, leaves other parameters untouched, and returns the binding hint. Record these
  - Observe on UNFIXED code: the picker's offered set from `isAravisCompatibleCamera` and the ICAM set from `isV4l2CompatibleCamera`; the five create-form type options; the `canManageDeviceCameras` role results for Operator / UseCaseAdmin / PortalAdmin (true) and DataScientist / Viewer / DataLabeler / undefined / null (false); the Cameras tab's normal synced render (cameras table, conflicts table, reachable static-image panel) and its loading and load-error early returns. Record these
  - Write property-based tests capturing the observed behavior in `src/pages/workflows/staticImageCameraPreservation.property.test.ts`: for any entry carrying a non-empty string `params.cameraId`, `cameraIdValue()` equals exactly that value even when a `capabilities.staticImage.id` is also present (the capabilities lookup never overrides params); for any entry carrying neither a usable `params.cameraId` nor a non-empty StaticImage capabilities id, `cameraIdValue()` is null and applying the selection retains the prior `camera_id` value or leaves the key absent; for any list of entries, the offered set from `isAravisCompatibleCamera` equals the pre-fix oracle exactly, and `isV4l2CompatibleCamera` agrees with its own oracle
  - Add a non-StaticImage guard case: an entry of type `Camera` that carries a `capabilities.staticImage.id` but no `params.cameraId` still resolves null and is still NOT offered by the Aravis picker (the fallback is scoped to `StaticImage`, so the offered set cannot widen)
  - Component-level preservation in `src/components/DeviceCamerasTab.staticImageFocus.test.tsx` (same file as task 1, separate describe block): arriving WITHOUT the focus flag renders exactly as today - no scroll, no flag - and the panel, the role gate, the five type options, and the loading and load-error early returns are unchanged
  - Note that the existing suites `src/pages/workflows/cameraReference.test.ts`, `src/pages/workflows/aravisCameraReference.property.test.ts`, `src/pages/deployments/CameraBindingMatrix.test.tsx`, and `src/components/DeviceCamerasTab.test.tsx` are themselves preservation coverage: their generators never emit a `capabilities` block, so they must keep passing untouched
  - Run the tests on UNFIXED code
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline behavior to preserve)
  - Mark task complete when the tests are written, run, and passing on unfixed code
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.8, 3.9, 3.10, 3.11_

- [x] 3. Fix for the Static_Image_Camera binding dead end and the pin panel discoverability dead end

  - [x] 3.1 Resolve the Aravis camera id from the StaticImage capabilities identity
    - `edge-cv-portal/frontend/src/pages/workflows/cameraReference.ts`: keep `cameraIdValue()`'s `params.cameraId` resolution first and unchanged, then fall back to a new file-local helper that reads `capabilities.staticImage.id` as a non-empty string
    - Scope the fallback to `camera.type === 'StaticImage'`. Rationale: `isAravisCompatibleCamera()` calls `cameraIdValue()` in its `Camera`-type arm, so a type-agnostic fallback would let a `Camera` entry carrying a StaticImage capabilities block become Aravis-compatible and widen the offered set (Requirement 3.4 and the existing Property 7 oracle). StaticImage is already unconditionally compatible, so gating costs nothing for this bug
    - Read the id from the capabilities block rather than hardcoding `'static-image-camera'`, so a future change to the device's fixed enumeration identity flows through. A StaticImage entry carrying no capabilities identity still resolves null and still retains any prior `camera_id` (Requirement 3.2)
    - Guard the capabilities lookup like `getCameraBindingHint` does - registry payloads are external input, so a null, array, or non-object `capabilities.staticImage`, or a non-string / empty `id`, resolves null instead of throwing
    - Do NOT change `isAravisCompatibleCamera()`, `isV4l2CompatibleCamera()`, `cameraDeviceValue()`, `applyCameraSelection()`, or the `gain` / `exposure` / hint logic in `applyAravisCameraSelection()`
    - Do NOT change `src/backend/camera_sync/inventory.py` or any device-side file, and do NOT change the deploy-time compatible set in `edge-cv-portal/backend/functions/deployments.py`
    - Caller audit (all four `cameraIdValue` consumers checked): `isAravisCompatibleCamera` (Camera arm only, behavior identical under the type gate); `applyAravisCameraSelection` (the intended fix site); the Aravis option description in `NodeConfigPanel.tsx` (StaticImage options now describe as `static-image-camera`, which is Requirement 2.4 and affects no other type); `CameraBindingMatrix.tsx` (uses only `isAravisCompatibleCamera`, so the deploy-time binding matrix is untouched)
    - _Bug_Condition: isBugCondition(X) part 1 - an offered Aravis source where staticImageCapabilityId(X.camera) is non-null and paramsCameraId(X.camera) is null_
    - _Expected_Behavior: cameraIdValue'(camera) = staticImageCapabilityId(camera) and applyAravisCameraSelection'(...).parameters.camera_id = that value - the Fix Checking property in bugfix.md_
    - _Preservation: params-first resolution, null-resolving sources retaining prior values, gain/exposure/hint semantics, and the picker compatibility sets all unchanged_
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 3.1, 3.2, 3.3, 3.4_

  - [x] 3.2 Make the static-image pin panel discoverable on arrival from the shortcut
    - `edge-cv-portal/frontend/src/pages/workflows/NodeConfigPanel.tsx` (shortcut at roughly lines 1305-1329): add one focus query parameter to the URL the shortcut opens, keeping the existing `usecase_id` and `tab=cameras` parameters, the new-tab `noopener` open, the disabled-until-a-device-is-chosen behavior, and the manual-entry hiding
    - `edge-cv-portal/frontend/src/pages/DeviceDetail.tsx`: read the focus parameter from the existing `searchParams` (the `?tab=` handling at roughly line 64 is already there) and pass it to `DeviceCamerasTab` as an OPTIONAL prop
    - Plumb it as a prop rather than calling `useSearchParams` inside `DeviceCamerasTab`: that component takes no router dependency today and its existing test renders it directly with no Router wrapper, so a hook there would break the existing suite. The prop must be optional so the current call sites and tests stay valid
    - `edge-cv-portal/frontend/src/components/DeviceCamerasTab.tsx`: when the prop is set, scroll the existing `static-image-panel` container into view once loading has resolved (attach a ref to a wrapper around the panel and call `scrollIntoView` through an optional call, since jsdom does not implement it) and flag the panel visually with a Cloudscape pattern already used in this file - an `Alert type="info"` inside the panel, or an equivalent `Header` badge - carrying its own `data-testid`
    - Optionally add a short inline note in the "Create camera source" modal pointing at the static-image panel for pinned test images, using the existing `Box` / `Alert` patterns
    - Do NOT add `StaticImage` to `CAMERA_TYPE_OPTIONS` - the backend rejects manual creation of discovery-managed sources - and do NOT change the `canManageDeviceCameras` gate or `DEVICE_MUTATION_ROLES`
    - Expected test update, intentional and not a regression: the existing assertion in `src/pages/workflows/NodeConfigPanel.test.tsx` pins the exact shortcut URL `'/devices/dev-1?usecase_id=uc-1&tab=cameras'`. Update it to the new URL and keep asserting the existing parameters, the `noopener` new-tab open, and the disabled-until-a-device-is-chosen behavior
    - _Bug_Condition: isBugCondition(X) part 2 - arrival at the device Cameras tab through the pin shortcut with staticImagePanelInView(X.landing) false_
    - _Expected_Behavior: staticImagePanelInView'(X.landing) is true - the panel is scrolled into view and flagged on arrival_
    - _Preservation: the shortcut's existing parameters and gating, the five create-form type options, the role gate, and the Cameras tab's normal render and early returns all unchanged_
    - _Requirements: 2.5, 2.6, 3.8, 3.10, 3.11_

  - [x] 3.3 Verify bug condition exploration test now passes
    - **Property 1: Expected Behavior** - StaticImage camera id resolves from capabilities and the pin panel is reachable
    - **IMPORTANT**: Re-run the SAME tests from task 1 - do NOT write new tests
    - The tests from task 1 encode the expected behavior; when they pass they confirm the picker's StaticImage selection binds `camera_id` and the pin panel is in view on arrival
    - Run the bug condition exploration tests from task 1
    - **EXPECTED OUTCOME**: Tests PASS (confirms both bugs are fixed)
    - _Requirements: 2.1, 2.2, 2.3, 2.4, 2.5, 2.6_

  - [x] 3.4 Verify preservation tests still pass
    - **Property 2: Preservation** - Existing id resolution, selection semantics, picker compatibility, and Cameras tab behavior unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 2 - do NOT write new tests
    - Run the preservation property tests from task 2, plus the existing suites `cameraReference.test.ts`, `aravisCameraReference.property.test.ts`, `CameraBindingMatrix.test.tsx`, and `DeviceCamerasTab.test.tsx`
    - Confirm `git diff` touches only the four frontend source files plus the new test files and the one intentional URL assertion update: no `src/backend/camera_sync/` file, no `edge-cv-portal/backend/` file, no infrastructure file
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - Confirm all tests still pass after the fix (no regressions)
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.7, 3.8, 3.9, 3.10, 3.11, 3.12_

- [x] 4. Checkpoint - Ensure all tests pass
  - Run the full frontend suite: `cd edge-cv-portal/frontend && npx vitest run`
  - Run the type check: `cd edge-cv-portal/frontend && npx tsc --noEmit`
  - Ensure all tests pass (the new binding, preservation, focus, and shortcut tests plus the entire existing suite) and the type check is clean; ask the user if questions arise

- [x] 5. Deploy frontend and verify live
  - **Frontend-only deploy** via `./deploy-frontend.sh` from `edge-cv-portal/` - safe end-to-end since the portal-deploy-flag-hardening spec; this fix touches four frontend files, so there is no backend or infrastructure change to deploy
  - Follow the `.kiro/steering/builds.md` gates first: `pgrep -af "gdk component build"` and `pgrep -af "build-custom.sh"` must both return nothing. Do NOT run a portal deploy while a component build is in progress
  - **STOP AND ASK THE USER BEFORE PINNING**: step (b) below mutates live device state on `jetson-thor1`, a real Jetson in use (it submits a pin request that changes what the device serves as its static-image camera). Get explicit confirmation first. If the user declines, run the read-only verification as the default and skip (b) and the parts of (c) that need a present camera
  - (a) Read-only: confirm the served bundle on `d23v4ltibogb5x.cloudfront.net` (account 164152369890, us-east-1) carries the capabilities-fallback logic - fetch the deployed `index-*.js` and check for the `staticImage` id lookup next to the `cameraId` resolution
  - (a2) Read-only: fetch the live inventory payload `GET /devices/jetson-thor1/cameras` (rest-api `yqvyoowugk`) and check the returned `static-image-camera` entry against the fixed resolution logic - the entry's `capabilities.staticImage.id` is what the picker now writes into `camera_id`
  - (b) Only after confirmation: pin a test image to `jetson-thor1` through the Portal pin API (upload-url, then pin) and confirm the status route reports `deviceReported.present: true` with the static camera no longer absent
  - (c) In the Workflow_Builder, add an `aravis_camera_source` node, choose `jetson-thor1` as the reference device, select Static Image Camera, and confirm the panel shows `camera_id` set to `static-image-camera` with the `Required parameter 'camera_id' has no value` violation gone
  - **Expect TWO static-image options at this point, and that is correct for this deploy**: Defect 3 (the duplicate registration) is device-side and lands only after the user's component build (task 10). Verify BOTH bind `camera_id` to `static-image-camera` — the `AravisDiscovered` duplicate "AWS-DDA Static Image Camera" from its `params.cameraId`, the `StaticImage` entry "Static Image Camera" from the new capabilities fallback (bugfix.md 3.20). Do NOT wait for the device fix and do NOT try to hide the duplicate in the frontend
  - (d) Click "Pin a static test image…" and confirm the Cameras tab lands with the "Static image camera" panel in view and flagged, with the pin controls reachable without hunting past the 9-row cameras table
  - Spot-check that the physical cameras on the device still bind as before (an `AravisDiscovered` source still populates its own camera id) and that the Absent badge still reflects the device-reported state
  - _Requirements: 2.2, 2.3, 2.5, 3.1, 3.5, 3.6, 3.7, 3.12_

- [x] 6. Write bug condition exploration test for the duplicate registration (device-side)
  - **Property 3: Bug Condition** - Duplicate Static_Image_Camera registration in the reported inventory
  - **CRITICAL**: These tests MUST FAIL on unfixed code - failure confirms the duplicate exists
  - **DO NOT attempt to fix the tests or the code when they fail**
  - **NOTE**: These tests encode the expected behavior - they will validate the fix when they pass after implementation
  - **GOAL**: Surface counterexamples that demonstrate one virtual camera is registered twice
  - **Independent of tasks 1-5**: this is the device track and can be written in parallel with the portal tests
  - **Conventions first**: the device suite uses **hypothesis** (not fast-check) and runs in the flask-app x86 container. READ `test/backend-test/camera_sync/test_property_pin_inventory.py` (generators, `_assert_fixed_identity`, the real temp-dir `StaticImageStore`), `pin_worker_support.py` (`FakeShadowAccessor`, `FakeClock`, `make_worker`, `fresh_store_dirs`), `test_build_inventory_aravis.py` (the `DiscoveredAravisCamera` / `InventorySnapshot` / `TrackedCamera` fixtures), and `test_pin_agent_wiring.py` (agent wiring with fakes) BEFORE writing anything, and follow their harness and hypothesis profiles (root conftest: `fast` = 25 examples, `HYPOTHESIS_PROFILE=ci` = 100)
  - New file: `test/backend-test/camera_sync/test_property_static_camera_duplicate_registration.py`
  - **Scoped PBT approach**: the defect is deterministic, so scope the property to the shipped shapes while generating around them. Build the discovery input the way `getCameras()` produces it while pinned - a fake bus camera object carrying `STATIC_IMAGE_CAMERA_IDENTITY` fed through the REAL `enumerate_aravis(enumerator=...)`, so the `arv-` stable id comes from the real derivation rather than a literal - mixed with arbitrary physical V4L2 and Aravis cameras and arbitrary configured Image_Sources, then merged with `static_image_pinned=True`
  - Assert the derived id: `aravis_stable_id(STATIC_IMAGE_CAMERA_IDENTITY["vendor"], ["model"], ["serial"], ["physical_id"]) == "arv-6c84191b7fe6"` - the live registry id, recomputed from the shipped constants (pin this so a future identity change is visible)
  - Assert `build_inventory(...)` reports EXACTLY ONE registration for the static camera, counting by the id a node binds to (`camera_source_id == STATIC_IMAGE_CAMERA_ID` OR `params.cameraId == STATIC_IMAGE_CAMERA_ID`), and that NO entry carries `camera_source_id == "arv-6c84191b7fe6"`. On unfixed code this reports two
  - Assert the same for the unpinned-after-reported state: the discovery snapshot still tracking the `arv-` camera as absent (`InventorySnapshot` + `TrackedCamera(absent=True)`) plus `static_image_pinned=False` and a `static_image_absent_since` timestamp yields exactly one ABSENT registration, and zero for the never-reported state (no timestamp)
  - Assert every non-static entry is byte-for-byte the pre-fix merge output (independent oracle over the same inputs with the static bus camera removed)
  - Include the exact live counterexample from `GET /devices/jetson-thor1/cameras` as a concrete case: `{"id":"arv-6c84191b7fe6","type":"AravisDiscovered","name":"AWS-DDA Static Image Camera","absent":true,"params":{"cameraId":"static-image-camera","address":"internal","protocol":"StaticImage","serial":"STATIC-IMAGE-0"}}` alongside `{"id":"static-image-camera","type":"StaticImage","absent":true,"params":{}}` - two rows for one camera
  - Migration half (Property 3, part 4): using `FakeShadowAccessor` with a reported state that ALREADY carries `cameras["arv-6c84191b7fe6"]`, assert the agent's next report retires that key explicitly (the key present with a `null` value in the written `reported.cameras`) and does so ONCE, not on every report. This fails on unfixed code, which never writes the key at all
  - Run in the flask-app x86 container following the `.kiro/steering/builds.md` pattern: `docker run --rm -v "$(pwd)":/repo -w /repo -e PYTHONPATH=/repo/src/backend:/repo/test/backend-test flask-app:latest bash -lc 'PY=$(command -v python3.11 || command -v python3.10); $PY -m pip install --no-cache-dir --quiet pytest hypothesis sarge testfixtures; $PY -m pytest test/backend-test/camera_sync/test_property_static_camera_duplicate_registration.py -q -p no:cacheprovider'`
  - **EXPECTED OUTCOME**: Tests FAIL (this is correct - it proves the duplicate exists)
  - Document counterexamples found (expected: two entries for the static camera - one `AravisDiscovered` under the derived `arv-6c84191b7fe6`, one `StaticImage` under `static-image-camera` - and no retirement key in the written report)
  - Mark task complete when the tests are written, run, and the failures are documented
  - _Requirements: 1.7, 1.8, 1.9, 1.10, 1.11, 2.7, 2.8, 2.9, 2.10, 2.11_

- [x] 7. Write preservation property tests for the device track (BEFORE implementing the fix)
  - **Property 4: Preservation** - Device enumeration, discovery mapping, and reporting unchanged
  - **IMPORTANT**: Follow observation-first methodology - run the UNFIXED code first, record the actual outputs, then assert those recorded outputs
  - New file: `test/backend-test/camera_sync/test_property_static_camera_dedup_preservation.py`
  - Observe on UNFIXED code and record: `build_inventory()` output for discovery results containing NO aravis camera whose `camera_id` is `static-image-camera` (arbitrary V4L2 + Aravis cameras, arbitrary configured sources, both pin states); the shape of the dedicated static entry (fixed id, `StaticImage`, `edge-discovered`, `params: {}`, identity under `capabilities.staticImage`, `discovered: True`) and of the absent variant (`absent=True` with the supplied `absentSince`)
  - Observe on UNFIXED code and record: `getCameras()` against a real temp-dir `StaticImageStore` appends the synthetic entry while pinned and omits it while unpinned (`test_property_pin_equivalence.py` already builds this identity tuple - reuse its approach), and `getCamera("static-image-camera")` returns the static handle while pinned and raises the not-found-with-pin-hint while unpinned
  - Observe on UNFIXED code and record: `aravis_stable_id()` and `enumerate_aravis()` outputs for physical identities, including a Fake-camera identity that maps to a single entry with `camera_id: "Fake_1"` - the live-healthy case (registry `arv-c9dd20f60ee1`, `Aravis Fake`, present, binds correctly), which must stay a single bindable entry
  - Observe on UNFIXED code and record: a configured Image_Source of type `Camera` with `cameraId: "static-image-camera"` merges into one `cfg-{imageSourceId}` entry carrying `capabilities.aravis` and the tracked absent state; this must be IDENTICAL after the fix (the exclusion applies only to the separately reported discovery entry, never to a user's configured source)
  - Observe on UNFIXED code and record: the agent's report document shape (`schemaVersion`, `reportedAt`, version counters, `failures`, `acks`, `aliases`, `discoveryErrors`) for an inventory with no static camera involved
  - Write hypothesis property tests asserting exactly those recorded behaviors, plus: for any discovery result with no static-image aravis camera, fixed output equals the pre-fix oracle exactly; and the binding-invariance assertion that both duplicates carried the same bindable id string `static-image-camera` (so de-duplication invalidates no existing workflow binding)
  - Note that these existing suites are themselves preservation coverage and MUST keep passing untouched: `camera_sync/test_build_inventory.py`, `camera_sync/test_build_inventory_aravis.py`, `camera_sync/test_property_pin_inventory.py`, `camera_sync/test_property_pin_equivalence.py`, `camera_sync/test_property_aravis_configured_discovered_merge.py`, `camera_sync/test_property_aravis_failure_isolation.py`, `camera_sync/test_report_timing.py`, `camera_sync/test_property_reconnect_catch_up.py`, `camera_sync/test_pin_*.py`, the whole `camera_discovery/` suite, and `static_image_camera/test_property_enumeration_resilience.py`
  - Run the tests on UNFIXED code in the same container invocation as task 6
  - **EXPECTED OUTCOME**: Tests PASS (this confirms the baseline behavior to preserve)
  - Mark task complete when the tests are written, run, and passing on unfixed code
  - _Requirements: 3.13, 3.14, 3.15, 3.16, 3.17, 3.18, 3.19_

- [x] 8. Fix for the duplicate Static_Image_Camera registration (device-side)

  - [x] 8.1 Exclude the aravis-enumerated static camera from the reported discovery entries
    - `src/backend/camera_sync/inventory.py`: in `build_inventory()`, skip any tracked `DiscoveredAravisCamera` whose `camera_id` equals `STATIC_IMAGE_CAMERA_ID` when emitting the discovered-only entries, so the merge's own dedicated entry is the single reported registration
    - Scope the exclusion to the discovered-only emission loop, NOT to the tracked index used by the configured merge: a configured Image_Source whose `cameraId` is `static-image-camera` must keep merging exactly as today (Requirement 3.18). Excluding it from `tracked` entirely would strip `capabilities.aravis` and the absent state off that user-configured entry
    - Derive the retired stable id once, from the shipped identity through `camera_discovery.aravis.aravis_stable_id()` (e.g. a module constant `STATIC_IMAGE_ARAVIS_STABLE_ID`), never hardcoding `arv-6c84191b7fe6` (Requirement 2.11). Match on `camera_id` (the semantic key) rather than on the derived id, so an identity change cannot silently un-match
    - Apply the exclusion in EVERY pin state, not only while pinned: after an unpin the tracker still holds the `arv-` entry as an absent leftover, and the dedicated entry's own explicit-absence lifecycle is what the Portal consumes (Requirement 2.8)
    - Update the module docstring to record the third finding, matching the style of the existing second-hardware-finding note
    - Do NOT change `src/backend/edge_ml1_p_camera_management/aravis_functions.py` (`getCameras()`, `rescan_cameras()`, `getCamera()` short-circuit), `src/backend/camera_discovery/aravis.py` (`aravis_stable_id()`, `_map_camera()`, `enumerate_aravis()`), the physical-camera absence tracking, `_static_image_entry()` / `_static_image_absent_entry()`, or any frontend or Portal backend file
    - _Bug_Condition: isBugCondition(X) part 3 - an inventory merge whose discovery input carries an aravis camera with camera_id = STATIC_IMAGE_CAMERA_ID and that reports more than one registration_
    - _Expected_Behavior: countStaticImageRegistrations(buildInventory'(X)) = 1 while pinned or unpinned-after-reported, 0 when never reported, with no arv-* entry in any state - Property 3 in bugfix.md_
    - _Preservation: device-local enumeration, the discovery mapping, the configured-source merge, the dedicated entry's shape, and every non-static entry unchanged_
    - _Requirements: 2.7, 2.8, 2.11, 3.13, 3.15, 3.17, 3.18_

  - [x] 8.2 Retire the already-published duplicate key once
    - `src/backend/camera_sync/agent.py`: when the derived `arv-` key was previously reported, include it ONCE in the written `reported.cameras` with a `null` value, which deletes the key from the shadow. Omission alone cannot work - shadow updates merge nested maps, so the key would stay alive in every documents event and the Portal's missing-from-report deletion path would never fire (bugfix.md 1.11)
    - Answer "previously reported" from the two independent records the existing `_static_previously_reported()` already uses - the start-time shadow reported-versions floor and the version state store - generalized to take a camera source id. Do not invent a third mechanism
    - Make it one-shot like the existing `acks` / `aliases` / `failures` consumption: emitted in a report, cleared only after that report is written successfully (so an offline retry still carries it), and pruned from the version state store so a restart does not re-emit it. A redundant re-emission is harmless (deleting an absent key is a no-op) but must not churn every report
    - The null never reaches the Portal parser: the documents event carries the post-merge state, from which the key is gone, so `_parse_record`'s "reported.cameras is not a map of objects" check is unaffected. Confirm this with the Portal-side shadow emulator, which already covers null-key deletion (`edge-cv-portal/backend/tests/test_camera_shadow_sync_integration.py` asserts a null write removes the key and later documents events stop resurrecting the entry) - a TEST-ONLY cross-check, no Portal deploy
    - Precedent to mirror, not to duplicate: `_clear_static_camera_shadow_key()` in `edge-cv-portal/backend/functions/camera_sync.py` does exactly this Portal-side for `static-image-camera`. Do NOT add a Portal-side variant for the `arv-` key - that would make Defect 3 need a Portal backend deploy too. Device-side keeps the portal track frontend-only
    - Fallback if the IPC shadow write turns out to drop nulls (the accessor `json.dumps`es the payload, so it should not): report the retired key explicitly `absent` instead and record the residual - a lingering absent phantom row - as a known limitation, then raise the Portal-side prune as a follow-up. Do not silently switch approaches; note it in the task and tell the user
    - _Bug_Condition: isBugCondition(X) part 4 - a report that omits an already-published duplicate key without retiring it_
    - _Expected_Behavior: carriesRetirement(reportWrite'(X), staticImageAravisStableId()) with retirementEmittedCount' = 1 - Property 3 in bugfix.md_
    - _Preservation: the report document's schemaVersion, version counters, failures, acks, aliases, discoveryErrors, and the pin worker's behavior all unchanged_
    - _Requirements: 2.9, 2.10, 3.19_

  - [x] 8.3 Verify the duplicate-registration exploration test now passes
    - **Property 3: Expected Behavior** - Single Static_Image_Camera registration in the reported inventory
    - **IMPORTANT**: Re-run the SAME tests from task 6 - do NOT write new tests
    - The tests from task 6 encode the expected behavior; when they pass they confirm exactly one registration in every pin state and the one-shot retirement of the already-published key
    - **EXPECTED OUTCOME**: Tests PASS (confirms the duplicate is gone)
    - _Requirements: 2.7, 2.8, 2.9, 2.10, 2.11_

  - [x] 8.4 Verify the device preservation tests still pass
    - **Property 4: Preservation** - Device enumeration, discovery mapping, and reporting unchanged
    - **IMPORTANT**: Re-run the SAME tests from task 7 - do NOT write new tests
    - Run the task 7 tests plus the existing suites listed there (`camera_sync/`, `camera_discovery/`, `static_image_camera/`)
    - Confirm `git diff` for this task touches only `src/backend/camera_sync/inventory.py`, `src/backend/camera_sync/agent.py`, and the two new device test files: no `aravis_functions.py`, no `camera_discovery/` file, no frontend file, no `edge-cv-portal/backend/` file, no infrastructure file
    - **EXPECTED OUTCOME**: Tests PASS (confirms no regressions)
    - _Requirements: 3.13, 3.14, 3.15, 3.16, 3.17, 3.18, 3.19, 3.20_

- [x] 9. Checkpoint - device suite green, no component build
  - Run the full device-side suites in the flask-app x86 container (the `.kiro/steering/builds.md` invocation pattern): `test/backend-test/camera_sync`, `test/backend-test/camera_discovery`, `test/backend-test/static_image_camera`
  - Run the security preservation guards and confirm they are green BEFORE the user starts any build: `python3 -m pytest test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py test/backend-test/security/preservation/test_preservation_secrets_out_of_scope_guard.py -p no:cacheprovider --noconftest -q`. Neither `camera_sync/inventory.py` nor `camera_sync/agent.py` is a preservation-tracked file, so no rebaseline is expected - verify rather than assume
  - Note for the user: `edge-cv-portal/infrastructure/cdk.out` must be moved aside before their build (the cdk.out drift guard), and a portal deploy must not run while a build is in progress
  - **Do NOT run `gdk component build` or `build-custom.sh` in this task** - builds take ~100 minutes and the user drives them
  - Ensure everything passes; ask the user if questions arise

- [ ] 10. Hand off the device build, then verify on-device (post-build)
  - **This task does NOT build or deploy anything.** It states exactly what the user needs to build and deploy, then verifies the result once they have
  - **What the user builds**: the LocalServer component for the target devices - `jetson-thor1` runs `aws.edgeml.dda.LocalServer.arm64JP7`, currently **1.0.24**, so the fix lands as the next patch (1.0.25). Per `.kiro/steering/builds.md`: set `gdk-config.json` to that component name, confirm `pgrep -af "gdk component build"` and `pgrep -af "build-custom.sh"` both return nothing, move `cdk.out` aside, confirm the preservation guards are green (task 9), then `gdk component build` and publish - one target at a time, never two builds at once, and no portal deploy while a build runs
  - **What the user deploys**: a Greengrass deployment revision pinning the new component version to `jetson-thor1` (and any other device that should get the fix; `dlap701` is the other SSH-reachable device - build the variant matching its architecture if it is included)
  - **Post-build on-device verification** (SSH access to `jetson-thor1` and `dlap701` is available through the user's remote path; no credentials belong in this spec). The backend container runs `network_mode: host`, so the LocalServer API answers on `http://127.0.0.1:5000` from the device:
    - (a) Read-only: `curl -s http://127.0.0.1:5000/cameras` - confirm the physical cameras still enumerate (currently `Fake_1` plus a Basler) and that while an image is pinned the static camera STILL appears here. Its presence in this list is the proof that `getCameras()` was left alone (Requirement 3.13); its absence would mean the fix was applied in the wrong place
    - (b) Read-only: `curl -s http://127.0.0.1:5000/static-image-camera/pin` - currently returns `{"pinned":false,"cameraId":"static-image-camera","metadata":null}`; confirm the shape and the pin state are unchanged
    - (c) Read-only: `GET /devices/jetson-thor1/cameras` on the deployed portal (account 164152369890, us-east-1, rest-api `yqvyoowugk`) - confirm exactly ONE static-image entry, that `arv-6c84191b7fe6` is gone (8 cameras where there were 9), and that the Fake camera is still a single present entry (`arv-c9dd20f60ee1`, `params.cameraId: "Fake_1"`)
    - (d) Read-only: the `dda-camera-registry` named shadow for `jetson-thor1` - confirm `reported.cameras` no longer carries the `arv-6c84191b7fe6` key (the retirement's null write deleted it) while `static-image-camera` is still reported
    - (e) In the Workflow_Builder: the Aravis picker offers exactly one Static Image Camera option, and selecting it binds `camera_id: "static-image-camera"` with no `Required parameter 'camera_id' has no value` violation
    - (f) Spot-check that a physical `AravisDiscovered` source still binds its own camera id and that the Absent badge still reflects the device-reported state
  - **STOP AND ASK THE USER BEFORE PINNING**: verifying the pinned state end to end (and step (a)'s pinned assertion) mutates live state on `jetson-thor1`, a real Jetson in use. Get explicit confirmation first. If the user declines, run the read-only checks in the unpinned state - which still verifies the de-duplication, since both duplicates were reported absent - and skip the pinned assertions
  - _Requirements: 2.7, 2.8, 2.9, 2.10, 3.13, 3.14, 3.15, 3.16, 3.17_

## Notes

- **Test-first ordering is mandatory on both tracks**: task 1 (bug condition) must FAIL and task 2
  (preservation) must PASS on the UNFIXED code before implementing task 3; task 6 must FAIL and task
  7 must PASS before implementing task 8. Do not modify `cameraReference.ts`, `NodeConfigPanel.tsx`,
  `DeviceDetail.tsx`, or `DeviceCamerasTab.tsx` until 1 and 2 are written and documented, and do not
  modify `camera_sync/inventory.py` or `camera_sync/agent.py` until 6 and 7 are.
- **Property references**: Property 1 (Bug Condition / Fix Checking, portal) validates Requirements
  2.1 through 2.6; Property 2 (Preservation, portal) validates 3.1, 3.2, 3.3, 3.4, 3.8, 3.9, 3.10,
  3.11; Property 3 (Bug Condition / Fix Checking, device) validates 2.7 through 2.11; Property 4
  (Preservation, device) validates 3.13 through 3.20. Requirements 3.5, 3.7, and 3.12 (the device
  entry SHAPE, the deploy-time compatible set, and the Portal routes unchanged) are enforced by the
  zero-diff constraints in tasks 3.4 and 8.4 and re-checked live in tasks 5 and 10. Requirement 3.6
  (the Absent badge is correct behavior, not a defect) is re-checked live in task 5.
- **Two independently shippable tracks**: the portal track (1-5) is frontend-only and deploys in
  minutes; the device track (6-10) needs a user-driven component build (~100 minutes) plus a
  deployment revision. Neither blocks the other. Do NOT gate the portal deploy on the device build,
  and do NOT paper over Defect 3 in the frontend while waiting - the duplicate is a device-side
  reporting defect and both duplicates bind correctly once the frontend fix lands (3.20).
- **Confirmed root causes (file evidence)**: `cameraIdValue()` resolves only `params.cameraId`
  (`cameraReference.ts` line ~366) while `applyAravisCameraSelection()` writes `camera_id` only on
  a non-null resolution (line ~380); the device reports the entry with `params={}` and the id under
  `capabilities.staticImage` (`src/backend/camera_sync/inventory.py`, `_static_image_entry()` /
  `_static_image_absent_entry()`), confirmed in the live `dda-camera-registry` named shadow for
  `jetson-thor1`. Defect 2: the shortcut opens the Cameras tab at the top of the page
  (`NodeConfigPanel.tsx` lines ~1305-1329, `DeviceDetail.tsx` line ~64) while `StaticImagePanel`
  renders below the cameras table (`DeviceCamerasTab.tsx`, defined line ~206, rendered line ~856)
  and the prominent create action offers five types that exclude StaticImage (line ~492).
  Defect 3: `getCameras()` appends `Camera(**STATIC_IMAGE_CAMERA_IDENTITY)` while pinned
  (`aravis_functions.py` line ~114), `enumerate_aravis()` / `_map_camera()` derive
  `aravis_stable_id("AWS-DDA", "Static Image Camera", "STATIC-IMAGE-0")` - recomputed from the
  shipped constants as exactly `arv-6c84191b7fe6`, the live registry id - and `build_inventory()`
  appends its own dedicated entry (`inventory.py` line ~279) with no exclusion of the
  aravis-enumerated one. Verified: the only `STATIC_IMAGE_CAMERA_ID` uses in `inventory.py` are the
  import, the name constant, and the two entry builders - there is no filter over the discovery
  cameras.
- **Shadow-persistence migration, decided**: an already-published duplicate cannot converge by
  omission. Shadow updates MERGE nested maps, so `arv-6c84191b7fe6` would stay in the shadow, every
  documents event would keep carrying it, and the Portal's `_deletion_candidates` path would never
  see it as missing - the same trap the second hardware finding hit for `static-image-camera`
  (`inventory.py` docstring, Requirement 6.2). Three routes were considered: (i) report the
  duplicate explicitly ABSENT forever, which converges its state but leaves a phantom
  "AWS-DDA Static Image Camera" row in the table and the picker, so it fails "exactly one entry";
  (ii) a one-time Portal-side cleanup, which works and would even converge pre-fix builds, but adds
  a Portal backend deploy to what is otherwise a pure device fix and duplicates a device-side
  responsibility; (iii) a device-side one-shot explicit shadow-key deletion (a null-valued key).
  **(iii) is chosen** - deletion, not absence, is the correct semantics for a registration that must
  cease to exist rather than a camera that might come back, and the mechanism is already proven in
  this system by the Portal's `_clear_static_camera_shadow_key`, whose integration test asserts the
  key is removed and later documents events stop resurrecting the entry. Once the key is gone, the
  Portal's EXISTING missing-from-report deletion path removes the registry entry with zero Portal
  change. **Observable end state for a device that already reported the duplicate (jetson-thor1
  has)**: nothing changes until the new component version is deployed; the first report from the
  fixed build deletes the shadow key, the next ingest deletes the registry entry, and the Portal then
  shows 8 cameras with a single `static-image-camera` row and a single picker option. Until then the
  duplicate stays exactly as it is today - and, after the portal track ships, both duplicates bind.
- **Scope guards**: the portal fix is frontend-only and touches four files; the device fix touches
  two backend files. `isAravisCompatibleCamera`, the deploy-time compatible set
  `{Camera, AravisDiscovered, StaticImage}`, the `CAMERA_TYPE_OPTIONS` list, the device-mutation role
  gate, `getCameras()` / `rescan_cameras()` / `getCamera()`, `aravis_stable_id()`, the Aravis mapping
  for real bus cameras, the physical-camera absence tracking, the dedicated static entry's shape, and
  the Portal backend are all deliberately left alone.
- **Deliberately out of scope**: when a user has explicitly created an Image_Source of type `Camera`
  with `cameraId: "static-image-camera"`, the registry holds that configured `cfg-` entry alongside
  the dedicated virtual entry. That is pre-existing shipped behavior for configured sources, it is
  not the phantom this fix removes, and task 8.1 preserves it verbatim (Requirement 3.18). If that
  pairing is also unwanted, it belongs in its own spec.
- **Verified not defects** (regression-prevention clauses, not fixes): the Absent badge on
  `jetson-thor1` is correct - the live status route reports an applied `remove` request with
  `deviceReported.present: false`, so nothing is pinned, and explicit absence reporting is intended
  behavior. The **Aravis Fake camera is healthy end to end** - device `Fake_1` enumerates on-device
  and reports as a single bindable registry entry (`arv-c9dd20f60ee1`, `Aravis Fake`, present,
  `params.cameraId: "Fake_1"`); the original report of a "missing fake camera" is explained by it
  being bus-discovered, so it never appears in the "Create camera source" type list, not by anything
  being broken. Requirement 3.16 pins it as regression prevention. The Portal plumbing is healthy -
  the cameras route returns synced with 9 cameras, the pin routes are live, the panel and its strings
  are present in the deployed bundle, and the pin controls are correctly gated on the device-mutation
  roles. The Cameras tab's loading and load-error early returns hide the panel but are not triggered
  in the normal synced state.
- **Deploy (task 5) is frontend-only and bounded** (npm build, S3 sync, CloudFront invalidation),
  but respect the builds.md sequencing gates - never overlap with a running component build. The
  live pin in step (b) mutates real device state and needs explicit user confirmation; read-only
  verification is the default. The same confirmation gate applies to the pinned assertions in task 10.
- **The device track never builds or deploys a component** (tasks 6-9 are tests and source edits;
  task 10 is a hand-off plus verification). Builds take ~100 minutes, corrupt each other if run
  concurrently, and are the user's to drive. Sequence: task 9 green → user moves `cdk.out` aside and
  confirms no build is running → user builds and publishes `aws.edgeml.dda.LocalServer.arm64JP7`
  (1.0.24 → next patch) → user deploys the revision to `jetson-thor1` → task 10's verification.
- **Risks**: (1) the retirement write depends on nulls surviving the Greengrass IPC shadow update -
  the accessor `json.dumps`es its payload, so it should, and task 8.2 states the explicit fallback and
  requires telling the user rather than silently switching approaches. (2) A device that never
  reported the duplicate must not receive a retirement write at all; the "previously reported" gate is
  what prevents pointless writes, and its two-source form is reused rather than reinvented. (3) The
  version state store keeps the retired key until pruned, so an unpruned key would re-emit the null on
  every restart - harmless (deleting an absent key is a no-op) but noisy, which is why 8.2 prunes it.
