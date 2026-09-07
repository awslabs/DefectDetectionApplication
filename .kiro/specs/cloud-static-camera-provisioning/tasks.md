# Implementation Plan: cloud-static-camera-provisioning

## Overview

Implementation in dependency order, portal core outward to the device, then rollout:

1. **Portal pure core** — `pin_requests.py` (Pin_Request lifecycle reducer, condition-guarded `pending -> {applied, failed, superseded}` transitions, status view), fully testable without AWS. Properties 7–8 pin it down first.
2. **Portal_Pin_API routes** — the four new routes in `edge-cv-portal/backend/functions/camera_registry.py` (presigned-PUT upload, pin submit with staged-object validation + CopyObject + `PIN_REQUEST#` item + `desired.staticImagePin` shadow write, removal, status). Properties 1–6 plus example tests.
3. **Ingest extension** — `camera_sync.py` routes `reported.staticImagePin` to the pin reducer; emulated-shadow end-to-end integration test.
4. **CDK** — `CameraRegistryApiStack` routes, S3 `static-image-pins/` grant, imaging layer + ≥1024 MB memory, staging lifecycle rule, with assertions.
5. **Device worker** — `src/backend/camera_sync/pin_worker.py` (marker-based idempotence, 3×120 s retrieval with ≥5 s spacing and sha256 verify, apply via `StaticImageStore.pin_bytes`/`unpin`, reported echo). Properties 9, 11–14.
6. **Device integration** — `EdgeSyncAgent` delta routing + startup catch-up + `MAX_REPORT_BYTES` 7168→6144, `build_inventory` static-camera entry. Properties 10, 15 plus wiring examples.
7. **Frontend** — `DeviceCamerasTab` static-image panel, Workflow_Builder picker shortcut.
8. **Rollout** — portal deploy first, then sequential JP5/JP6/JP7 component builds, then mandatory on-device verification, honoring the workspace `builds.md` rule throughout (never deploy the portal concurrently with a component build; move `cdk.out` aside and run the guard suite green before each build).

Property-based tests use hypothesis, minimum 100 examples each (do not lower `max_examples`), one property per test, tagged `# Feature: cloud-static-camera-provisioning, Property {N}: {title}`. Properties 1–8 live in `edge-cv-portal/backend/tests/`; Properties 9–15 in `test/backend-test/camera_sync/`. **No preservation-tracked file changes** (no recipe, docker-compose, Dockerfile, `requirements.txt`, or `setup_station.sh` edits), so no security baselines need rebaselining — but the guard suite still runs before each build.

## Tasks

- [x] 1. Portal pure core: Pin_Request lifecycle
  - [x] 1.1 Create `edge-cv-portal/backend/functions/pin_requests.py`
    - `PIN_REQUEST#{createdAtMs:014d}#{uuid8}` SK scheme (lexicographic SK order = creation order; most recent = highest SK, query `begins_with` + `ScanIndexForward=False`)
    - `reduce_pin_confirmation(pin_item, reported, now_ms)`: `applied` on a `pending` item records device metadata (width, height, format, fileName) + confirmation timestamp; `failed` records device-reported reason + timestamp; unknown requestId or non-`pending` item is a no-op; idempotent under duplicate delivery
    - Persistence helpers enforcing the single `pending -> {applied, failed, superseded}` transition with a DynamoDB `ConditionExpression` on `status = pending`; best-effort canonical-S3-object delete on any terminal transition (no time-based expiry anywhere)
    - Supersede helper: transition the current `pending` item (if any) to `superseded` before a new submission's item is written
    - Status view builder: `latest` = most recent non-superseded state (superseded excluded from current state, retained in `history`); `noPinRequest: true` for zero items; `deviceMetadata` included exactly when the most recent pin-type request is `applied`; `connectivity` (`connected`/`disconnected`) included while `latest.status == "pending"`; `deviceReported` (from the `CAMERA#static-image-camera` registry entry) presented as current state even when it disagrees with the recorded outcome
    - _Requirements: 4.1, 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 5.3, 5.6, 5.7, 1.7, 1.10, 2.6, 7.5_

  - [x]* 1.2 Write property test for Pin_Request lifecycle and supersede
    - **Property 7: Pin_Request lifecycle and supersede**
    - **Validates: Requirements 4.1, 5.1, 5.3, 5.6, 2.6**
    - New module `edge-cv-portal/backend/tests/test_pin_requests_lifecycle_properties.py`; hypothesis over sequences of submissions/confirmations/failure-reports/supersedes against fake DynamoDB + recorded fake S3: at most one `pending` at any point; at most one transition out of `pending`, never a second; desired slot equals the newest request's document after each submission; canonical object deleted only on leaving `pending`; no transition without a triggering event

  - [x]* 1.3 Write property test for confirmation reduction and status view
    - **Property 8: Confirmation reduction and status view**
    - **Validates: Requirements 4.2, 4.3, 4.4, 4.5, 4.6, 4.7, 4.8, 1.7, 1.10, 5.7, 7.5**
    - New module `edge-cv-portal/backend/tests/test_pin_requests_status_properties.py`; hypothesis over item sets and device reports: applied/failed reduction records metadata/reason + timestamp; status view returns the latest-created item's id/status/op/createdAt, excludes superseded from current state but keeps them in history, reports deviceMetadata iff most recent pin-type request is `applied`, includes connectivity exactly while pending, presents device-reported state as current on disagreement, and returns a no-request response (not an error) for the empty set

- [x] 2. Portal_Pin_API routes
  - [x] 2.1 Implement the four routes in `edge-cv-portal/backend/functions/camera_registry.py`
    - `POST /devices/{id}/cameras/static-image/upload-url` (`MANAGE_DEVICES`): presigned PUT for `static-image-pins/staging/{uuid}`, 15-minute TTL
    - `POST /devices/{id}/cameras/static-image/pin` (`MANAGE_DEVICES`): resolve Use_Case only from Portal-side records (devices table first, registry items second — never the caller's query parameter); 404 "device not registered" before any side effect; HeadObject size check (≤ 50 MB) then download + Pillow decode + format ∈ {JPEG, PNG, BMP} (mirror `StaticImageStore` constants, decompression-bomb guard enabled); sha256; CopyObject to `static-image-pins/{device_id}/{pin_request_id}` before any shadow write; supersede any pending request; write `PIN_REQUEST#` item as `pending`; replace `desired.staticImagePin` wholesale (fileName truncated to 128 chars, serialized section ≤ 1024 bytes enforced before writing); S3/shadow-step failures transition the item to `failed` with an error identifying the failing step; staged object deleted on rejection; response `{pinRequestId, deviceId, status: "pending"}`
    - `DELETE /devices/{id}/cameras/static-image/pin` (`MANAGE_DEVICES`): removal Pin_Request (`op: "remove"`, no transport object), same lifecycle + desired-slot replacement
    - `GET /devices/{id}/cameras/static-image` (`VIEW_DEVICES`): status response from the 1.1 view builder + `device_connectivity_status` mapped to exactly `connected`/`disconnected`
    - Audit: `pin_static_image` / `remove_static_image` via `log_audit_event` (acting user, device, op, pin request id, timestamp) on acceptance; standard `unauthorized_access` event with attempted operation type on denial; denials return 403 naming the required permission with zero side effects
    - _Requirements: 1.1, 1.2, 1.3, 1.4, 1.5, 1.6, 1.8, 1.9, 1.10, 2.1, 2.2, 2.3, 2.4, 2.5, 5.1, 5.3, 7.1, 7.2, 8.1, 8.2, 8.3, 8.4, 8.5, 8.6, 8.7_

  - [x]* 2.2 Write property test for submission validation acceptance
    - **Property 1: Submission validation acceptance**
    - **Validates: Requirements 1.1, 1.3, 1.4**
    - New module `edge-cv-portal/backend/tests/test_pin_submission_validation_properties.py`; hypothesis over byte payloads (Pillow-generated valid images + undecodable bytes) with an injectable size limit straddling the boundary: accepted iff decodable Supported_Image_Format and within limit; rejections name the formats or the limit; rejected submissions record zero Pin_Request items, zero S3 canonical writes, zero shadow writes

  - [x]* 2.3 Write property test for accepted-submission effects
    - **Property 2: Accepted-submission effects**
    - **Validates: Requirements 1.2, 1.5, 2.1, 2.2, 2.3, 2.4, 7.2, 8.4**
    - New module `edge-cv-portal/backend/tests/test_pin_submission_effects_properties.py`; hypothesis over accepted pin/removal submissions: exactly one `pending` item scoped to the device; for pins, content stored strictly before any shadow write; desired document carries reference + sha256 + size/format metadata, never image bytes, serialized ≤ 1024 bytes; response carries id/device/`pending`; exactly one audit event with user, device, op, request id, timestamp

  - [x]* 2.4 Write property test for delivery-initiation failure
    - **Property 3: Delivery-initiation failure**
    - **Validates: Requirements 1.9, 2.5**
    - New module `edge-cv-portal/backend/tests/test_pin_delivery_failure_properties.py`; hypothesis over injected store-step and shadow-step failures: request ends `failed`, operator error identifies the failing step, store-step failure records zero shadow writes

  - [x]* 2.5 Write property test for unknown device rejection
    - **Property 4: Unknown device rejection**
    - **Validates: Requirements 1.8, 8.7**
    - New module `edge-cv-portal/backend/tests/test_pin_unknown_device_properties.py`; hypothesis over device ids with no Portal record across pin/replace/removal: not-registered error, zero items, zero transport writes, zero shadow writes

  - [x]* 2.6 Write property test for authorization decisions and audit
    - **Property 5: Authorization decisions and audit**
    - **Validates: Requirements 8.1, 8.2, 8.3, 8.6**
    - New module `edge-cv-portal/backend/tests/test_pin_authorization_properties.py`; hypothesis over permission-grant sets × operations: mutations succeed iff `MANAGE_DEVICES` for the device's use case, status iff `VIEW_DEVICES`; every denial names the required permission, has zero side effects and zero status data, and logs `unauthorized_access` with user, device, attempted op, timestamp

  - [x]* 2.7 Write property test for use-case resolution ignoring caller scoping
    - **Property 6: Use-case resolution ignores caller scoping**
    - **Validates: Requirements 8.5**
    - New module `edge-cv-portal/backend/tests/test_pin_usecase_resolution_properties.py`; hypothesis over devices-table value × caller-supplied parameter × grants: the authorization outcome depends only on the devices-table value

  - [x]* 2.8 Write route wiring and structural example tests
    - New module `edge-cv-portal/backend/tests/test_pin_routes_examples.py`
    - Route wiring: 404s, OPTIONS/CORS, malformed bodies, missing staging key
    - Req 1.6: each Pin_Request is associated with exactly one Target_Device (structural)
    - Req 6.5: generic Camera_Registry mutation route rejects csid `static-image-camera` with the existing discovery-managed rejection, entry unchanged
    - _Requirements: 1.6, 6.5_

- [x] 3. Ingest extension and shadow round-trip integration
  - [x] 3.1 Extend `edge-cv-portal/backend/functions/camera_sync.py`
    - `_parse_record` additionally extracts `reported.staticImagePin` (tolerant — absent section means nothing to do; camera path untouched)
    - `_process_report` calls the 1.1 pin reducer; malformed pin sections are logged and skipped without affecting camera reduction (section isolation); duplicate documents-event re-reduction is a no-op
    - _Requirements: 4.1, 4.2, 4.3, 4.8, 5.6, 7.5, 7.6_

  - [x]* 3.2 Write ingest routing unit tests
    - New module `edge-cv-portal/backend/tests/test_camera_sync_pin_ingest.py`
    - Confirmation routes to the pin reducer; missing section is a no-op; malformed `reported.staticImagePin` is isolated from the camera path; confirmation for a superseded/terminal request id changes nothing
    - _Requirements: 4.1, 4.8, 5.6_

  - [x]* 3.3 Write emulated-shadow end-to-end integration test
    - Extend the `edge-cv-portal/backend/tests/test_camera_shadow_sync_integration.py` harness: portal pin submit → desired slot populated → fake device confirmation document → item transitions to `applied` with metadata → `reported.cameras` including `static-image-camera` upserts the registry entry; removal round trip marks the entry absent
    - _Requirements: 1.2, 4.2, 6.1, 6.2, 7.5_

  - [x]* 3.4 Write deployment-binding example tests with a static registry entry
    - One example each against the existing deployment validation paths: absent `static-image-camera` entry produces the standard absence warning requiring confirmation (Req 6.7); missing entry produces the standard missing-source rejection naming the camera and device (Req 6.8)
    - _Requirements: 6.4, 6.7, 6.8_

- [x] 4. CDK infrastructure
  - [x] 4.1 Extend `CameraRegistryApiStack` (edge-cv-portal/infrastructure)
    - Register the four routes under the imported `/devices/{id}` resource (route salt rolls a new API deployment)
    - Camera_Registry Lambda: grant on the component bucket's `static-image-pins/*` prefix (Get/Put/Copy/Delete + presign), attach the existing imaging layer (Pillow), memory ≥ 1024 MB for the 50 MB decode
    - Component-bucket lifecycle rule expiring `static-image-pins/staging/` objects after 1 day (the `dda_labeling` preview-prefix precedent)
    - Bundle `pin_requests.py` into the Lambda asset (the `camera_sync.py` precedent)
    - _Requirements: 1.1, 2.6, 2.7_

  - [x]* 4.2 Write CDK assertions
    - Extend `edge-cv-portal/infrastructure/test/`: the four routes exist on the API; Lambda has the prefix-scoped S3 grant, the imaging layer, and the memory setting; the staging lifecycle rule exists (Req 2.7's infrastructure half)
    - _Requirements: 2.7_

- [x] 5. Checkpoint - Portal suite green
  - Run `python -m pytest edge-cv-portal/backend/tests -q` and the infrastructure assertions; ensure all tests pass, ask the user if questions arise.

- [x] 6. Device worker
  - [x] 6.1 Create `src/backend/camera_sync/pin_worker.py`
    - `StaticImagePinWorker(iot_shadow_accessor, thing_name, shadow_name, store_factory=get_store, s3_client_factory=None, marker_path=None, clock=time.monotonic, sleep=time.sleep)` — all collaborators injectable (the `EdgeSyncAgent` testing pattern)
    - `on_desired(desired)`: queue the desired document, newest replaces any queued older one (local single-slot mirror); processing on a dedicated daemon thread so downloads never block camera-report scheduling
    - `process_one(desired)`: marker check → (pin) retrieval with the retry policy → apply → marker write → reported echo; returns the reported document
    - Retry policy: ≤ 3 attempts, each bounded at 120 s (botocore connect/read timeouts + wall-clock bound on the streamed read), ≥ 5 s between attempts; sha256 computed over streamed bytes and compared before any store call; mismatch discards bytes and counts as a failed attempt; download aborts past 50 MB (defense in depth); after the third failure, no store call and `status: "failed"` naming the final attempt's cause (`retrieval failure: …` / `checksum mismatch`)
    - Apply: exactly `store.pin_bytes(data, fileName)` for pin/replace; `store.unpin()` for removal, mapping the "no image is pinned" error to a successful no-op confirmation
    - Idempotence marker at `$COMPONENT_WORK_PATH/static_image_camera/applied_pin_request.json` (`{requestId, op, status, metadata, completedAtEpochMs}`), atomic temp+`os.replace` write; matching requestId re-reports the recorded outcome without re-executing; corrupt/missing marker treated as no marker
    - Reported echo written only after the store call returns and the marker is written: verbatim echo of every desired field + `status`/`reason`/`metadata`/`completedAtEpochMs` (echo equality silences the delta); merge-safe top-level shadow write (never clobbers `reported.cameras`)
    - After any terminal outcome, call the agent's `report_inventory()`
    - _Requirements: 2.8, 2.9, 2.10, 2.11, 3.1, 3.4, 3.5, 3.6, 5.4, 5.5, 7.3, 7.4, 7.7, 7.8_

  - [x]* 6.2 Write property test for retrieval verification and retry
    - **Property 9: Retrieval verification and retry**
    - **Validates: Requirements 2.8, 2.9, 2.10, 2.11, 5.5**
    - New module `test/backend-test/camera_sync/test_property_pin_retrieval_retry.py`; hypothesis over served bytes × declared checksum × injected failure patterns (timeouts, errors, mismatched bytes) with fake S3 client and fake clock/sleep: apply only on checksum match; every failed attempt discards bytes and counts; ≤ 3 attempts with ≥ 5 s spacing; on total failure the store is never invoked (prior state byte-identical) and the report is `failed` naming the final cause

  - [x]* 6.3 Write property test for failure retaining the prior image
    - **Property 11: Failure retains the prior image**
    - **Validates: Requirements 3.4, 7.8**
    - New module `test/backend-test/camera_sync/test_property_pin_failure_retention.py`; hypothesis over prior pinned states × failing applications (checksum-valid undecodable bytes, storage failure, failing replace/removal) over a temp-dir `StaticImageStore`: pinned content, pin status, and `get_frame` unchanged; report carries `failed` with a descriptive reason

  - [x]* 6.4 Write property test for idempotent redelivery
    - **Property 12: Idempotent redelivery**
    - **Validates: Requirements 3.5, 7.4**
    - New module `test/backend-test/camera_sync/test_property_pin_idempotence.py`; hypothesis over requests (pin and removal, including removals on an unpinned store) × delivery counts N ≥ 1: store mutations invoked at most once; pin state, enumeration state, and reported status identical after every delivery

  - [x]* 6.5 Write property test for confirmation ordering
    - **Property 13: Confirmation ordering**
    - **Validates: Requirements 3.6, 7.7**
    - New module `test/backend-test/camera_sync/test_property_pin_confirmation_ordering.py`; hypothesis over pin cycles with instrumented store/shadow seams: the reported confirmation (id + applied metadata) is written only after `pin_bytes` returns; frame grabs before the store operation return the previous content byte-for-byte

  - [x]* 6.6 Write property test for newest-request-wins on the device
    - **Property 14: Newest-request-wins on the device**
    - **Validates: Requirements 5.4, 7.3, 7.4**
    - New module `test/backend-test/camera_sync/test_property_pin_newest_wins.py`; hypothesis over sequences of ≥ 2 desired documents (offline-then-reconnect model): only the newest operation executes, zero superseded requests applied, state converges to the newest request's requested state; worker removal leaves the store exactly as a direct `unpin` does, no-op confirm on already-unpinned

- [x] 7. Device agent and inventory integration
  - [x] 7.1 Extend `src/backend/camera_sync/agent.py`
    - Construct and own the `StaticImagePinWorker`; `on_delta` routes `state.staticImagePin` → `pin_worker.on_desired(...)` (existing `state.changes` routing unchanged)
    - `start()`'s existing shadow GET also inspects `desired.staticImagePin` and hands it to the worker when its `requestId` differs from the marker (startup/reconnect catch-up within the 60 s bound)
    - `MAX_REPORT_BYTES` from `7 * 1024` to `6 * 1024` (pin-section headroom; truncation ladder untouched)
    - `_load_inventory` passes `static_image_pinned=get_store().is_pinned()` to `build_inventory`
    - _Requirements: 5.2, 5.4, 2.3, 6.1_

  - [x] 7.2 Extend `src/backend/camera_sync/inventory.py` and the apply-path guard
    - `build_inventory` gains optional `static_image_pinned: bool`; when true, append the `static-image-camera` `CameraSourceState` (name "Static Image Camera", type `StaticImage`, origin `edge-discovered`, `staticImage` capabilities from identity + pin metadata, `discovered=True`); when false, the entry is simply absent (existing absence handling applies); all other entries identical to the pre-feature merge; version counters via the existing `version_state` store
    - Device-side apply path's `disc-`/`cfg-` prefix guard extended to treat the literal `static-image-camera` id as discovery-managed (defense in depth)
    - _Requirements: 6.1, 6.2, 6.5, 7.6_

  - [x]* 7.3 Write property test for cloud/device pin equivalence
    - **Property 10: Cloud/device pin equivalence**
    - **Validates: Requirements 3.1, 3.2, 3.3, 3.7, 3.8, 7.1**
    - New module `test/backend-test/camera_sync/test_property_pin_equivalence.py`; model-based hypothesis over valid image files: worker application vs direct Device_Pin_API pin of the same file — identical on-disk store state, pin-status metadata, camera-enumeration identity fields, byte-identical `get_frame` output, and identical observable results for subsequent Device_Pin_API replace/remove

  - [x]* 7.4 Write property test for inventory presence tracking the pin state
    - **Property 15: Inventory presence tracks the pin state**
    - **Validates: Requirements 6.1, 6.2, 6.5, 7.6**
    - New module `test/backend-test/camera_sync/test_property_pin_inventory.py`; hypothesis over configured Image_Sources × discovery snapshots × pinned flag: exactly one `static-image-camera` entry iff flagged, carrying origin `edge-discovered` and the fixed identity; zero entries otherwise; all other entries identical to the pre-feature merge; flag reflects store state regardless of pin origin

  - [x]* 7.5 Write agent wiring and headroom example tests
    - New module `test/backend-test/camera_sync/test_pin_agent_wiring.py`
    - `on_delta` routing (`state.staticImagePin` → worker; `state.changes` untouched); startup catch-up handoff (Req 5.2 wiring: unprocessed desired document reaches the worker on `start()`); marker corruption recovery (treated as no marker); report-size headroom (a full camera report + both pin sections stays ≤ 8 KB with `MAX_REPORT_BYTES = 6144`)
    - _Requirements: 5.2, 3.5, 2.3_

- [x] 8. Checkpoint - Device suite green (host, then container)
  - Run `test/backend-test/camera_sync` and `test/backend-test/static_image_camera` on the host venv, then in the flask-app container exactly as the design specifies (interpreter differs by image — python3.11 on JP5, python3.10 on JP6):
    ```
    docker run --rm -v "$(pwd)":/repo -w /repo \
      -e PYTHONPATH=/repo/src/backend:/repo/test/backend-test \
      flask-app:latest bash -lc \
      'PY=$(command -v python3.11 || command -v python3.10); \
       $PY -m pip install --no-cache-dir --quiet pytest sarge testfixtures hypothesis; \
       $PY -m pytest test/backend-test/camera_sync test/backend-test/static_image_camera -q -p no:cacheprovider'
    ```
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Portal frontend
  - [x] 9.1 Add the static-image panel to `edge-cv-portal/frontend/src/components/DeviceCamerasTab.tsx`
    - "Static image camera" panel: Sync_Status badge, device-reported pinned state, metadata (width/height/format/fileName), failure reason, connectivity hint while pending, no-request state
    - Upload-and-pin flow (file input → upload-url → presigned PUT → pin submit), replace (same flow), remove; actions gated on the user's mutation permission; poll the status route while `pending` (the tab already polls the registry)
    - _Requirements: 1.5, 1.7, 1.10, 4.4, 4.5, 4.6, 4.7, 7.2_

  - [x] 9.2 Add the Workflow_Builder picker shortcut
    - `edge-cv-portal/frontend/src/pages/workflows/` camera reference picker: "Pin a static test image…" affordance routing to the target device's Cameras tab (device chooser first); no picker listing logic changes — the camera appears through the registry like any camera
    - _Requirements: 6.3_

  - [x]* 9.3 Write frontend component tests
    - Per existing frontend test conventions (1–3 examples each): panel renders each Sync_Status + no-request + failure reason + connectivity hint; pin/replace/remove flows call the right routes; actions hidden without the mutation permission; picker shows a registry-backed `static-image-camera` entry like any camera (Req 6.3) and the binding matrix offers it (Req 6.4)
    - _Requirements: 1.10, 4.4, 4.7, 6.3, 6.4_

- [x] 10. Portal rollout
  - [x] 10.1 Deploy the portal (infrastructure + frontend)
    - Verify no component build is running first (`pgrep -af "gdk component build"` / `pgrep -af "build-custom.sh"`) — never deploy concurrently with a build
    - Run `deploy-infrastructure.sh` then `deploy-frontend.sh`; after the deploy fully finishes, move `edge-cv-portal/infrastructure/cdk.out` aside (`mv cdk.out cdk.out.bak-$(date +%Y%m%dT%H%M%SZ)`)
    - Portal-first rollout is safe: pins to devices on the old component stay `pending` until the new component arrives
    - _Requirements: 1.2, 2.6, 5.1_

- [x] 11. Component builds (sequential, per workspace builds rule)
  - [x] 11.1 Build the LocalServer component for JP5, JP6, and JP7 — strictly one at a time
    - Pre-flight before each build: no running build (`pgrep` checks above); preservation guard suite green (`python3 -m pytest test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py test/backend-test/security/preservation/test_preservation_secrets_out_of_scope_guard.py -p no:cacheprovider --noconftest -q`) — never assume it; `cdk.out` moved aside (task 10.1)
    - No preservation-tracked file changes in this feature, so no baseline rebaselining is expected
    - Build sequentially via `run_jp_builds.sh` (e.g. `TARGETS="5 6 7" ./run_jp_builds.sh`) or the portal build fleet (note: the JP6 fleet-build path was used for the base static-image-camera feature); each target is a full GPU onnxruntime build, ~1–2 h; capture per-target logs (`.gdk_build_jp5.log` / `.gdk_build_jp6.log` / `.gdk_build_jp7.log`); restore `gdk-config.json` when done
    - _Requirements: 3.1, 5.2 (ships the device-side change)_

- [ ] 12. On-device verification (manual — workspace builds rule, mandatory before commit)
  - [ ]* 12.1 Verify the full portal→device matrix on real hardware, every affected arch
    - Manual: cannot be executed by a coding agent from this environment; per the workspace `builds.md` rule it is mandatory before the device-side change is committed — unit and container tests are necessary but not sufficient
    - Deploy the built component to real devices of each affected arch (JP5 and/or JP6/JP7 as deployed — do not assume one arch implies another)
    - Pin from the Portal → confirm on device (`GET /static-image-camera/pin`, camera enumeration, frame grab through a workflow) and in the Portal (status `applied` + metadata, registry entry, Workflow_Builder picker, Camera_Binding_Matrix)
    - Replace → new content served, status applied; Remove → unpinned, registry entry absent, absence warning on binding
    - Offline case: pin while the device is down → applies within ~60 s of reconnect (Req 5.2); supersede case: two pins while offline → only the newest lands, older item `superseded` (Reqs 5.3, 5.4)
    - Transport authorization (Req 2.7's runtime half): canonical object GET succeeds with device TES credentials, is rejected without
    - Confirm the backend stays healthy for a sustained period (no crash, no container restart, no crash-loop; camera reports still flowing)
    - State in the commit/PR what was verified on which device(s)
    - _Requirements: 2.7, 5.2, 5.3, 5.4, 3.2, 3.7, 6.3, 6.4_

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP, except: the property suites are the design's primary correctness evidence, and task 12.1 is marked optional only because it cannot be executed by a coding agent — per the workspace `builds.md` rule it is mandatory before the device-side change is committed
- Each task references specific requirements for traceability
- Property tests: hypothesis, minimum 100 examples (do not lower `max_examples`), one property per test, tagged `# Feature: cloud-static-camera-provisioning, Property {N}: {title}`; Properties 1–8 portal-side, 9–15 device-side
- No preservation-tracked file changes (no recipe, docker-compose, Dockerfile, `requirements.txt`, `setup_station.sh`), so no security-preservation baselines need rebaselining — the guard suite still runs before every build (task 11.1)
- Sequencing constraints from `builds.md`: never two component builds at once; never a portal deploy concurrently with a component build; portal deploy → move `cdk.out` aside → builds
- Reqs 6.6 (zero node-catalog/schema changes) is structural — verified by review and the existing schema suites staying green; Req 5.2's 60-second bound and Req 2.7's runtime enforcement are verified on device (task 12.1)
- **Rollout record (tasks 10.1 / 11.1)**: portal deployed via `deploy-infrastructure.sh` + `deploy-frontend.sh` (EdgeCVPortalComputeStack + CameraRegistryApi nested stack updated with the new routes/grants/lifecycle; frontend deployed with CloudFront invalidation); regenerated `cdk.out` moved aside (`cdk.out.bak-*`), preservation guard suite green (4 passed, 3 skipped) and full preservation suite green in the flask-app container (138 passed, 8 skipped). Component build: **JP6 only, conscious scope decision** — the verification device fleet is JP6 (same decision as the base static-image-camera feature). Feature commit rebased and pushed as `8b8cb60` on `integration/all-specs`, built by Build_Job `1a27d261-b00a-45b7-b782-5a802cf6ec6a` on the dedicated JP6 Build Server (succeeded), published `aws.edgeml.dda.LocalServer.arm64JP6` **1.0.65**. JP5/JP7 deferred; build through the same fleet path when needed.
- **Reqs 6.3/6.4 gap fix (under task 9/3.4 scope)**: two compatibility filters excluded the `StaticImage` entry from `aravis_camera_source` nodes even though the device serves the Static_Image_Camera through the same aravis frame-feed path (proven in the base spec's executor tests). Fixed by adding `StaticImage` to `_CAMERA_COMPATIBLE_SOURCE_TYPES['aravis_camera_source']` (`edge-cv-portal/backend/functions/deployments.py`) and to `isAravisCompatibleCamera` (`edge-cv-portal/frontend/src/pages/workflows/cameraReference.ts`, which also feeds the CameraBindingMatrix aravis rows). Pinned type sets consciously re-recorded in `test_camera_source_removal_completeness.py` and the `test_property_aravis_type_compatibility.py` oracle; new examples in `test_camera_binding_validation.py` (TestStaticImageCameraBindings), `cameraReference.test.ts`, `aravisCameraReference.property.test.ts`, `NodeConfigPanel.test.tsx`, and `CameraBindingMatrix.test.tsx`. Backend modules green (77 passed, HYPOTHESIS_PROFILE=ci); frontend vitest green (88 passed) + `tsc --noEmit` clean.
- **Shadow-merge removal staleness bug (second finding from task 12.1 on-device verification, jetson-thor1 / LocalServer.arm64JP7 1.0.23)**: after a portal removal Pin_Request was confirmed applied (store unpinned, echo `applied`), the device's next full inventory report OMITTED `static-image-camera` from `reported.cameras` — but AWS IoT shadow updates MERGE nested maps, so the omitted key persisted in the shadow document, every subsequent documents event still carried the stale entry, and the portal reducer kept upserting it as present; the registry entry never went absent (**Req 6.2 violated**). The design's "when unpinned the entry is simply absent from the full report, so the existing absence handling applies" assumption is wrong against real shadow merge semantics (the emulated-shadow harness's removal test faked a device reporting the entry explicitly absent, so merge semantics were never exercised and test 3.3 passed silently). Fixes: **(1) portal-side mitigation (live once the portal deploys — works with deployed 1.0.23)**: `camera_sync.py` ingest, on a remove confirmation transitioning to `applied`, marks `CAMERA#static-image-camera` absent with the confirmation timestamp (after the camera reduction, so it wins within the same event over the stale merged entry) and best-effort writes `reported.cameras.static-image-camera: null` via the use-case iot-data client (`iot_data_client` seam; CameraSyncHandler role already carries `iot:UpdateThingShadow` on `thing/*` + `sts:AssumeRole` to `DDAPortalAccessRole` + the shared layer — no CDK change); reported deletions of the static camera absence-mark instead of deleting. **(2) device-side contract fix (rides the next component build)**: `build_inventory` gains `static_image_absent_since`; an unpinned, previously reported camera is reported explicitly ABSENT with a stable `absentSince` (remove-marker `completedAtEpochMs` when cloud-initiated, else wall clock at first absent observation; re-seeded from the shadow at start) — the discovered-camera absence pattern the reducer already consumes. Property 15 consciously updated (unpinned-after-reported now yields exactly one ABSENT entry, not zero). Tests: `test_camera_sync_pin_ingest.py` (applied-remove convergence class), harness merge-semantics guard tests + reworked removal round trips (deployed-build convergence path AND fixed-build absence-reporting path) in `test_camera_shadow_sync_integration.py`, device wiring tests in `test_pin_agent_wiring.py`.
- **Partial-delta starvation bug (found in task 12.1 on-device verification, jetson-thor1 / LocalServer.arm64JP7 1.0.23)**: the first portal pin applied in <1 s, but every pin→pin replace hung `pending` forever. AWS IoT computes the shadow delta **per-field** against the reported state, so the device's echo of request 1 made `op` and `bucket` equal to request 2's desired values — the delivered delta was PARTIAL (no `op`, no `bucket`) and the worker read `op=''` and never confirmed. Design Decision 1's "echo equality silences the delta" reasoning missed that partial deltas also starve later requests of unchanged fields. Two fixes: **(1) portal-side mitigation** — `write_desired_pin` (`camera_registry.py`) now clears `reported.staticImagePin` (null) in the SAME shadow update that writes the desired slot, so the next delta carries every desired field; clearing loses nothing (the prior confirmation was already ingested via the documents event, and the ingest treats an absent reported section as a no-op) — this covers **already-deployed device builds** immediately on portal deploy. **(2) device-side robustness** — `pin_worker.py process_one` detects partial documents (missing `op`, or `op=='pin'` missing any of bucket/key/sha256), fetches the CURRENT full desired document via the shadow accessor GET, uses it iff its requestId matches the delivery's, and otherwise reports `failed` naming the incomplete delivery (never a silent hang; newest-wins preserved — the GET returns the current slot, definitionally the newest). The device-side fix **rides the next component build**; until then the portal mitigation alone resolves the hang. Tests: Property 2 extended to pin the same-update echo clear (`test_pin_submission_effects_properties.py`); new device regression suite `test_pin_partial_delta.py` (partial-delta replace property, remove→remove example, GET-failure/swallowed-error/requestId-mismatch fallbacks, no-GET-on-complete-document).
- **Task 12.1 verification record (jetson-thor1, JP7, LocalServer.arm64JP7 1.0.23)**: both hardware-found bug fixes committed as `e0746cd` (rebased onto `integration/all-specs`, pushed) and the portal redeployed via `deploy-infrastructure.sh` (EdgeCVPortalComputeStack UPDATE_COMPLETE; `cdk.out` moved aside; preservation guard suite green, 4 passed / 3 skipped). Live re-verification after the deploy, all through the real Portal_Pin_API routes: fresh pin `01788799666303#1c06b802` applied in ~450 ms with correct deviceMetadata (PNG 320x240, reverify-a.png) and `deviceReported.present=true`; removal `01788799763846#1a508936` applied in ~120 ms AND converged (registry `CAMERA#static-image-camera` -> `absent=True`, `absent_since=1788799763966`; stale `reported.cameras.static-image-camera` shadow key cleared by the portal mitigation; canonical `static-image-pins/jetson-thor1/*` S3 prefix empty). This closes the previously stuck removal `01788796309828#f9f4ca20` state. VERIFIED on device: pin, pin->pin replace (earlier, ~500 ms, supersede of the stuck request observed), remove with Req 6.2 absence convergence. STILL PENDING on device: offline-pin (~60 s reconnect bound, Req 5.2), offline supersede matrix (Reqs 5.3/5.4), transport-authorization negative check (Req 2.7 runtime half), workflow frame-grab + picker/binding-matrix UI pass, sustained-health soak, and re-verification of the device-side fixes (partial-delta GET resolution + explicit absent reporting) once the dispatched JP7 fleet build (Build_Job `8d9ed11b-dffa-45dd-b513-3a94e062b74c`, component `aws.edgeml.dda.LocalServer.arm64JP7`, source_ref `integration/all-specs` @ `e0746cd`) publishes and deploys.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "4.1", "6.1", "7.2", "9.1", "9.2"] },
    { "id": 1, "tasks": ["1.2", "1.3", "2.1", "3.1", "4.2", "6.2", "6.3", "6.4", "6.5", "6.6", "7.1", "7.3", "7.4", "9.3"] },
    { "id": 2, "tasks": ["2.2", "2.3", "2.4", "2.5", "2.6", "2.7", "2.8", "3.2", "3.3", "3.4", "7.5"] },
    { "id": 3, "tasks": ["10.1"] },
    { "id": 4, "tasks": ["11.1"] },
    { "id": 5, "tasks": ["12.1"] }
  ]
}
```
