# Implementation Plan: Camera Registry Sync

## Overview

Implementation spans three codebases. The pure cores are built first because everything else hangs off them: the Camera_Discovery enumeration and diff, the `build_inventory` merge, the `reduce_report` sync reducer, the `validate_camera_bindings` deployment validator, and the `resolve_bindings` device-side resolver. Portal infrastructure (DynamoDB table, IoT rule + SQS + Lambda wiring, API routes) proceeds in parallel; the Lambdas, frontend views, and edge wiring follow, ending with integration tests and backward-compatibility verification.

Test baselines that must stay green throughout: portal backend `edge-cv-portal/backend/tests` (moto-backed conftest stack, 883 passing), infrastructure jest (30 passing) plus `npx tsc --noEmit` clean, frontend vitest (423 passing) plus `npm run build`, and LocalServer `test/backend-test` run with `PYTHONPATH=src/backend:test/backend-test` (204 passing + 3 skipped). Python property tests use `hypothesis` and TypeScript property tests use `fast-check`, each configured for a minimum of 100 iterations and tagged `**Feature: camera-registry-sync, Property {number}: {property_text}**`.

## Tasks

- [x] 1. Implement edge camera discovery (`src/backend/camera_discovery/`)
  - [x] 1.1 Implement the V4L2/CSI enumeration core
    - New module `src/backend/camera_discovery/` with frozen `DiscoveredCamera` dataclass (`stable_id`, `device_path`, `card_name`, `bus_info`, `driver`, `kind`, `formats`) and `CameraDiscovery.enumerate() -> DiscoveryResult`
    - Glob `/dev/video*`, issue `VIDIOC_QUERYCAP` / `VIDIOC_ENUM_FMT` / `VIDIOC_ENUM_FRAMESIZES` via `fcntl.ioctl` behind an injectable ioctl layer so tests supply fake devices; skip nodes without the `VIDEO_CAPTURE` capability; classify Tegra CSI drivers as `kind="csi"`; stable id `disc-{sha1(bus_info+card)[:12]}`
    - Per-node failures recorded in `DiscoveryResult.failures` with enumeration continuing; total failure yields an empty result plus a logged error, never an exception
    - _Requirements: 2.1, 2.2, 2.6, 11.2_

  - [x] 1.2 Write property test for discovery enumeration completeness
    - **Feature: camera-registry-sync, Property 2: Discovery enumeration completeness**
    - **Validates: Requirements 2.1, 2.2**
    - Hypothesis over generated fake capture devices; run under `PYTHONPATH=src/backend:test/backend-test` in `test/backend-test`

  - [x] 1.3 Write property test for discovery failure isolation
    - **Feature: camera-registry-sync, Property 3: Discovery failure isolation**
    - **Validates: Requirements 2.6**

  - [x] 1.4 Implement the periodic re-enumeration loop and snapshot diff
    - `CameraDiscovery.start(interval_seconds=300, on_change=...)` / `stop()`; interval configurable through the existing feature-configs mechanism
    - Pure diff over consecutive `DiscoveryResult` snapshots: stable ids present before and missing now are emitted as absent with an absence timestamp; ids are never dropped from the tracked set; `on_change` fires only when the inventory changed
    - _Requirements: 2.3, 2.4_

  - [x] 1.5 Write property test for absence marking on re-enumeration
    - **Feature: camera-registry-sync, Property 4: Absence marking on re-enumeration**
    - **Validates: Requirements 2.4**

  - [x] 1.6 Write unit tests for discovery interval configuration
    - Default 300 s interval, feature-config override honored, `on_change` suppressed when consecutive enumerations are identical (fake clock)
    - _Requirements: 2.3_

- [x] 2. Implement the Edge_Sync_Agent (`src/backend/camera_sync/`)
  - [x] 2.1 Implement the build_inventory pure merge and local version state
    - Pure `build_inventory(image_sources, discovery_result) -> list[CameraSourceState]`: an Image_Source whose resolved device path equals a discovered camera's path yields one entry (configured id `cfg-{imageSourceId}`, configured params, discovered capability metadata, origin `edge-configured`, `discovered: true`); discovered-only hardware yields origin `edge-discovered`; no device path appears in two entries
    - Per-source version counters persisted in `/aws_dda/camera_sync_state.json` (atomic temp-file + rename), bumped on content-hash change; corrupt/missing state file re-floors versions from the shadow's current reported state
    - Image_Sources read only through the existing `ImageSourceAccessor` — no SQLite schema or accessor changes
    - _Requirements: 1.1, 2.5, 11.3_

  - [x] 2.2 Write property test for the configured/discovered merge
    - **Feature: camera-registry-sync, Property 5: Configured/discovered merge**
    - **Validates: Requirements 2.5**

  - [x] 2.3 Implement the report path over the dda-camera-registry shadow
    - `EdgeSyncAgent` using the existing `IoTShadowAccessor` (shadow name `dda-camera-registry`, thing name from `AWS_IOT_THING_NAME`) and the MQTT `SubscriptionHandler` pattern for delta notifications
    - Every report is the complete current inventory in the reported-document shape from the design (schemaVersion, cameras keyed by stable id, failures, discoveryErrors); capability metadata truncated with `capabilitiesTruncated` when a report would exceed 7 KB
    - Report triggers: LocalServer start (full report), Image_Source CRUD via `agent.report_inventory()` called from the existing FastAPI route layer, discovery `on_change`, and portal-change application; debounced to one shadow write per 5 s window (within the 30 s bound); failed writes retried with exponential backoff so the first post-reconnect write is automatically the complete catch-up state
    - _Requirements: 3.1, 3.3, 3.4, 12.4_

  - [x] 2.4 Write property test for reconnect catch-up publication
    - **Feature: camera-registry-sync, Property 6: Reconnect publishes complete current state**
    - **Validates: Requirements 3.3**
    - Hypothesis over interleaved inventory changes and failing shadow writes against a fake shadow accessor

  - [x] 2.5 Write unit tests for report timing
    - Debounce window and 30 s bound with a fake clock; startup full report; report trigger on Image_Source CRUD hook
    - _Requirements: 3.1, 3.4_

  - [x] 2.6 Implement the portal-change apply path (on_delta)
    - Apply each `desired.changes[csid]` through `InputConfigurationAccessor` / `ImageSourceAccessor`, preserving schema validation, camera-manager side effects, and DIO handling; success reports the applied state with `portal_change_id` echoed in `ack`; `ValidationError`/`HTTPException` reports `{csid, status: "failed", reason}` with the message verbatim; changes targeting origin `edge-discovered` refused with reason `discovery-managed`; applied or failed desired entries cleared by writing `null`
    - _Requirements: 5.2, 5.3, 5.4, 5.6, 11.3_

  - [x] 2.7 Write property test for the portal change apply/report round trip
    - **Feature: camera-registry-sync, Property 7: Portal change apply/report round trip**
    - **Validates: Requirements 5.2, 5.3, 5.4**
    - Hypothesis over portal changes applied against an in-memory SQLite device database, reduced through the portal reducer (import from the backend module or mirror the reducer contract)

  - [x] 2.8 Wire the agent into server_setup.py with failure isolation
    - Start `CameraDiscovery` and `EdgeSyncAgent` on a daemon thread from `server_setup.py` behind a top-level exception guard; construction or start failure is logged and leaves the rest of LocalServer startup untouched; no migration — the agent only reads existing tables through accessors
    - _Requirements: 11.2, 11.4_

  - [x] 2.9 Write agent isolation and no-migration tests
    - A raising agent constructor/start does not propagate into server setup; smoke test: agent started against a populated fixture SQLite database leaves every existing row byte-identical
    - _Requirements: 11.2, 11.4_

- [x] 3. Checkpoint - edge discovery and sync agent complete
  - Ensure all tests pass (`PYTHONPATH=src/backend:test/backend-test`, baseline 204+3skip preserved), ask the user if questions arise.

- [x] 4. Provision portal infrastructure (CDK, `edge-cv-portal/infrastructure`)
  - [x] 4.1 Add camera registry infrastructure to the CDK stacks
    - DynamoDB table `dda-portal-camera-registry` (PK `device_id`, SK item-type-prefixed `CAMERA#`/`META`/`CONFLICT#`, GSI `usecase-index` on `usecase_id`) following the existing `dda-portal-*` conventions
    - SQS shadow-report queue with DLQ; `camera_sync.py` Lambda with SQS event source; `camera_registry.py` Lambda with API Gateway routes (`GET/POST /devices/{id}/cameras`, `PUT/DELETE /devices/{id}/cameras/{csid}`, `GET /devices/{id}/cameras/conflicts`, `POST .../conflicts/{cid}/reapply`, `POST .../cameras/refresh`); deployments Lambda granted `iot-data` shadow write for `dda-camera-bindings`
    - IoT topic rule on `$aws/things/+/shadow/name/dda-camera-registry/update/documents` routed to the queue (cross-account queue policy), provisioned through the existing ComputeStack/use-case onboarding handlers
    - _Requirements: 1.1, 1.4, 3.2, 12.4_

  - [x] 4.2 Write infrastructure tests
    - Jest assertions for the table schema/GSI, queue + DLQ wiring, Lambda event sources, routes, and the IoT rule/queue policy in the onboarding template; `npx tsc --noEmit` clean, jest baseline 30 preserved
    - _Requirements: 1.1, 3.2_

- [x] 5. Implement the Portal_Sync_Service (`edge-cv-portal/backend/functions/camera_sync.py`)
  - [x] 5.1 Implement the reduce_report pure reducer
    - `reduce_report(registry_entry, incoming, now_ms) -> SyncOutcome` with actions `upsert | discard_stale | conflict`: version-guarded staleness discard; ack matching `portal_change_id` marks synced; failure entries mark failed with reason; conflict exactly when a pending portal change is unacknowledged and the edge content differs from the pending content, edge wins with a ConflictEvent carrying both versions, resolution, and timestamp; a reported deletion while a portal update is pending resolves as deletion-retained with a ConflictEvent
    - Reducer is idempotent under duplicate/out-of-order delivery; every processed report stamps the device `META` item (`last_report_at`, clears `never_synced`)
    - _Requirements: 3.2, 3.5, 5.3, 5.4, 6.1, 6.2, 6.3, 6.5_

  - [x] 5.2 Write property test for the sync reducer round trip
    - **Feature: camera-registry-sync, Property 1: Sync reducer round trip with version guard**
    - **Validates: Requirements 1.1, 1.2, 1.4, 3.2, 3.5**
    - Hypothesis generators per the design: multi-source inventories, version regressions, all origins and sync statuses, whitespace/unicode names, degenerate registries

  - [x] 5.3 Write property test for conflict classification
    - **Feature: camera-registry-sync, Property 8: Conflict classification with edge-wins resolution**
    - **Validates: Requirements 6.1, 6.2, 6.3, 6.5**

  - [x] 5.4 Implement the SQS ingest handler
    - Lambda handler consuming shadow-documents events from the queue, applying `reduce_report` per camera source and persisting outcomes (camera items, META, CONFLICT items) to `dda-portal-camera-registry` scoped to the device's `usecase_id`; malformed or unparseable reports logged and dead-lettered without blocking the batch
    - _Requirements: 1.4, 3.2_

  - [x] 5.5 Write unit tests for ingest behavior
    - Duplicate and out-of-order SQS delivery idempotency; malformed report dead-lettering; META stamping and `never_synced` clearing; moto-backed conftest stack
    - _Requirements: 3.2, 3.5_

- [x] 6. Implement the Camera_Registry API (`edge-cv-portal/backend/functions/camera_registry.py`)
  - [x] 6.1 Implement the read routes
    - `GET /devices/{device_id}/cameras` (Viewer): registry entries plus META, per-entry `stale` computed against the Staleness_Threshold, absent flag with `absent_since`, IoT connectivity status from the existing device-status lookup; `never_synced` devices return `{state: "never-synced"}`, not an empty list
    - `GET /devices/{device_id}/cameras/conflicts` (Viewer): conflict events newest first
    - Authorization via the existing use-case permission checks against the device's `usecase_id`; out-of-scope requests get the standard 403
    - _Requirements: 1.3, 1.5, 1.6, 4.1, 4.2, 4.4, 6.3, 12.1_

  - [x] 6.2 Implement the mutation, conflict-reapply, and refresh routes
    - `POST/PUT/DELETE` camera routes (Operator): create origin `portal-created` / update / pending-delete; write the shadow `desired.changes` entry first, then mark the registry entry `pending` with a fresh `portal_change_id`; reject mutations of origin `edge-discovered` with `DISCOVERY_MANAGED`; assumed-role shadow client failures return 502 with registry state untouched
    - `POST .../conflicts/{cid}/reapply` (Operator): re-issue the overridden portal version as a new pending change; `POST .../cameras/refresh` (Viewer): on-demand `GetThingShadow` via `get_usecase_client` running the same reducer
    - All mutating routes call `log_audit_event` with acting user, device, camera source, and timestamp
    - _Requirements: 5.1, 5.6, 5.7, 6.4, 12.2, 12.3_

  - [x] 6.3 Write property test for portal mutations
    - **Feature: camera-registry-sync, Property 9: Portal mutation produces pending state and matching desired document**
    - **Validates: Requirements 5.1**

  - [x] 6.4 Write property test for discovery-managed immutability
    - **Feature: camera-registry-sync, Property 10: Discovery-managed sources are immutable from the Portal**
    - **Validates: Requirements 5.6**

  - [x] 6.5 Add the staleness threshold setting
    - Settings table entry `camera_registry.staleness_threshold_hours` (default 24) readable by the cameras route and editable by PortalAdmin through the existing settings API
    - _Requirements: 4.3_

  - [x] 6.6 Write unit tests for the registry API
    - RBAC matrix per route (Viewer read, Operator mutate, cross-use-case 403 with `unauthorized_access` audit event); never-synced response shape; staleness boundaries at exactly/just past the threshold; disconnected-status pass-through; settings default; absent display fields; conflict re-apply; audit event payload per mutating route
    - _Requirements: 1.5, 1.6, 4.1, 4.2, 4.3, 4.4, 5.7, 6.4, 12.1, 12.2, 12.3_

- [x] 7. Checkpoint - portal sync and registry API complete
  - Ensure all tests pass (backend baseline 883 preserved, jest 30, tsc clean), ask the user if questions arise.

- [x] 8. Implement the device cameras view (frontend)
  - [x] 8.1 Implement the device detail cameras view
    - Cameras section on the device detail view listing name, type, parameters, capability metadata, origin, sync status (with failure reason), and last-reported timestamp; stale and absent badges with timestamps; device disconnected indicator; explicit "never synced" state; conflict event list with re-apply action; create/edit/delete forms for portal-managed sources (discovery-managed sources read-only); refresh-now button hitting the refresh route
    - _Requirements: 1.3, 1.6, 4.1, 4.2, 4.4, 5.4, 6.3, 6.4_

  - [x] 8.2 Write frontend tests for the cameras view
    - Vitest component tests: field display, stale/absent/disconnected/never-synced rendering, discovery-managed edit blocking, conflict re-apply flow
    - _Requirements: 1.3, 1.6, 4.1, 4.2, 4.4, 5.6, 6.4_

- [x] 9. Implement the Workflow_Builder camera picker (frontend)
  - [x] 9.1 Implement the camera reference control in NodeConfigPanel
    - For the `camera_source` node's `device` parameter (keyed off node type + parameter name, no `workflow_core` schema change): reference-device selector over the current Use_Case's devices, camera dropdown fed by `GET /devices/{id}/cameras` showing name, type, path/URL, sync status, and staleness badge; selection populates the node's `device` (plus gain/exposure when present) and stores `data.cameraBindingHint = { cameraSourceId, cameraName, sourceDeviceId }`; "manual entry" toggle retains the plain text input; the hint stays advisory node data so the definition remains device-portable
    - _Requirements: 7.1, 7.2, 7.3, 7.4, 7.5_

  - [x] 9.2 Write property test for camera selection application
    - **Feature: camera-registry-sync, Property 11: Camera selection populates the node and records the hint**
    - **Validates: Requirements 7.2**
    - fast-check, `numRuns: 100`, over generated Camera_Sources applied to a Camera_Input_Node

  - [x] 9.3 Write frontend tests for the picker
    - Picker population and display fields, staleness badges, manual entry toggle, hint persistence in node data
    - _Requirements: 7.1, 7.3, 7.4, 7.5_

- [x] 10. Extend the Component_Packager (`edge-cv-portal/backend/functions/workflow_packaging.py`)
  - [x] 10.1 Emit bindingPoints and record the version-item discriminator
    - For each Camera_Input_Node (type `camera_source` or a Custom_Node_Type with the new optional `camera_backed: true` descriptor flag), append a `bindingPoints` entry to `compiled_pipeline.json`: nodeId, nodeType, bindingHint from the definition, rendered default parameters, and arch-specific `slots` (v4l2src `device` arg on x86_64/x86_64_nvidia; `adapterBinding: true` with empty slots on JP4/JP5; CSI sensor selection on JP6); compiled elements keep their fully rendered defaults
    - Record `has_binding_points: true` and `camera_input_nodes` (node id, node type, binding hint, per-arch compiled device paths) on the workflow version item in DynamoDB
    - _Requirements: 8.6, 11.5_

  - [x] 10.2 Write property test for binding-hint transparency
    - **Feature: camera-registry-sync, Property 12: Binding hints are transparent to validation and compilation**
    - **Validates: Requirements 7.5, 11.5**
    - Hypothesis: validate and compile hinted definitions versus hint-stripped equivalents

  - [x] 10.3 Write packaging snapshot tests
    - Extend `test_workflow_generation.py`/packaging suite: packaging a camera workflow produces the same rendered segments as before plus the `bindingPoints` section; packaging a workflow without camera nodes is byte-identical to pre-feature output
    - _Requirements: 11.1, 11.5_

- [x] 11. Extend the Deployment_Service (`edge-cv-portal/backend/functions/deployments.py`)
  - [x] 11.1 Implement the validate_camera_bindings pure function
    - `validate_camera_bindings(version_item, targets, registry_snapshot, bindings, confirmed) -> (errors, warnings)`
    - Errors: unbound Camera_Input_Node on any target when `has_binding_points`; referenced `cameraSourceId` absent from the target's registry; Camera_Source type incompatible with the node type; override values violating the node type's declared parameter constraints via the existing `workflow_core` parameter validator
    - Warnings requiring matching `confirmed_warnings` ids: bound source stale/absent/pending/failed; `never_synced` target restricted to manual override; legacy path check when `has_binding_points` is false comparing compiled-in device paths against the target registry (warnings only, never errors)
    - Distinct bindings per device for the same node accepted; versions with no Camera_Input_Nodes produce no errors or warnings
    - _Requirements: 8.3, 8.4, 8.7, 8.8, 8.9, 9.1, 9.2, 9.3, 9.4, 9.5, 11.1_

  - [x] 11.2 Write property test for binding completeness validation
    - **Feature: camera-registry-sync, Property 13: Binding completeness validation**
    - **Validates: Requirements 8.3, 8.7, 8.9**

  - [x] 11.3 Write property test for binding existence validation
    - **Feature: camera-registry-sync, Property 14: Binding existence validation**
    - **Validates: Requirements 9.1, 9.2**

  - [x] 11.4 Write property test for degraded-source warning gating
    - **Feature: camera-registry-sync, Property 15: Degraded-source warnings gate submission on confirmation**
    - **Validates: Requirements 8.8, 9.3**

  - [x] 11.5 Write property test for type and override constraint validation
    - **Feature: camera-registry-sync, Property 16: Type compatibility and override constraint validation**
    - **Validates: Requirements 8.4, 9.4**

  - [x] 11.6 Write property test for the legacy compiled-path warning
    - **Feature: camera-registry-sync, Property 17: Legacy compiled-path warning**
    - **Validates: Requirements 9.5, 11.1**

  - [x] 11.7 Implement the binding context endpoint and deployment submission
    - Binding context endpoint returning per-target Camera_Sources for each Camera_Input_Node with hint-matching pre-selection; `create_workflow_deployment` accepts `camera_bindings` and `confirmed_warnings`, runs `validate_camera_bindings` alongside the existing pre-submit gates, and rejects with `REGISTRY_UNAVAILABLE` when the registry read fails rather than skipping validation
    - On success, write `desired.bindings["{workflowId}/{version}"]` into each target thing's `dda-camera-bindings` shadow (assumed-role iot-data client), prune keys for versions no longer deployed, then create the Greengrass deployment with the artifact untouched; a mid-submission shadow write failure aborts deployment creation and best-effort prunes already-written targets; bindings stored on the workflow-deployment record; audit event on creation
    - _Requirements: 8.1, 8.2, 8.5, 8.6, 12.3_

  - [x] 11.8 Write property test for hint pre-selection
    - **Feature: camera-registry-sync, Property 18: Binding hint pre-selection**
    - **Validates: Requirements 8.5**

  - [x] 11.9 Write property test for binding delivery
    - **Feature: camera-registry-sync, Property 19: Binding delivery round trip**
    - **Validates: Requirements 8.2, 8.6**
    - Hypothesis: desired documents written to a fake shadow client decode back to the submitted bindings per device; the packaged artifact bytes are unchanged by submission

  - [x] 11.10 Write unit tests for deployment submission behavior
    - Partial shadow-write failure aborting with target pruning; `REGISTRY_UNAVAILABLE` rejection; audit event payload; binding storage on the deployment record
    - _Requirements: 8.6, 12.3_

- [x] 12. Implement the CreateDeployment binding matrix (frontend)
  - [x] 12.1 Add the binding matrix step to CreateDeployment.tsx
    - Nodes × target devices matrix shown only when the selected workflow version has Camera_Input_Nodes (skipped entirely otherwise); per-cell camera dropdown from the binding context endpoint with hint pre-selection subject to user confirmation; per-cell manual-override entry; never-synced targets warn and restrict to manual override; warning confirmation checkboxes feeding `confirmed_warnings`; validation errors surfaced identifying the node and device
    - _Requirements: 8.1, 8.4, 8.5, 8.7, 8.8, 8.9, 9.2, 9.3_

  - [x] 12.2 Write frontend tests for the binding matrix
    - Matrix rendering per node/device, hint pre-selection, manual override entry, warning confirmation flow, skip when no Camera_Input_Nodes
    - _Requirements: 8.1, 8.5, 8.8, 8.9, 9.3_

- [x] 13. Checkpoint - packaging, deployment binding, and frontend complete
  - Ensure all tests pass (backend 883 baseline, frontend vitest 423 baseline, `npm run build`), ask the user if questions arise.

- [x] 14. Implement Workflow_Engine binding resolution (`src/backend/workflow_engine/`)
  - [x] 14.1 Implement the resolve_bindings pure function
    - New `src/backend/workflow_engine/camera_binding.py`: `resolve_bindings(document, bindings, local_inventory) -> ResolutionResult` (substituted document copy, status resolved|invalid, missing list, adapter_assignments)
    - `cameraSourceId` bindings look up the local inventory (same `build_inventory` stable ids) and substitute resolved parameter values into declared `slots`; `override` bindings substitute directly, constraint-checked against the vendored catalog descriptor; JP4/5 adapter binding points produce `adapter_assignments`; documents without `bindingPoints` or with no bindings supplied are returned unchanged
    - _Requirements: 10.1, 10.3, 10.5, 11.1_

  - [x] 14.2 Write property test for device-side binding resolution
    - **Feature: camera-registry-sync, Property 20: Device-side binding resolution**
    - **Validates: Requirements 10.1, 10.2, 10.3**

  - [x] 14.3 Write property test for no-binding identity
    - **Feature: camera-registry-sync, Property 22: No-binding identity**
    - **Validates: Requirements 10.5, 11.1, 11.5**
    - Generators include legacy documents lacking `bindingPoints` entirely

  - [x] 14.4 Implement the CameraBindingStore and watcher integration
    - `CameraBindingStore` reading the `dda-camera-bindings` shadow via `IoTShadowAccessor`, cached, refreshed on shadow delta; `WorkflowWatcher._register` fetches bindings for `{workflow_id}/{version}` and calls `resolve_bindings`; unresolved `cameraSourceId` marks the registration invalid with reason `missing camera source {csid}` so the existing invalid-registration path rejects triggers; unreadable bindings shadow marks binding-point documents invalid with reason `bindings unavailable` while documents without binding points register as today; invalid registrations re-resolved on discovery `on_change` and bindings-shadow delta, flipping to registered when everything resolves
    - _Requirements: 10.2, 10.4, 11.1_

  - [x] 14.5 Write property test for registration re-evaluation
    - **Feature: camera-registry-sync, Property 21: Registration re-evaluation on camera appearance**
    - **Validates: Requirements 10.4**

  - [x] 14.6 Write unit tests for watcher binding behavior
    - Bindings shadow unreadable: binding-point documents invalid, legacy documents register unchanged; trigger rejection for invalid registrations; pre-feature compiled documents register and execute with compiled-in values exactly as before
    - _Requirements: 10.2, 11.1_

- [x] 15. Integration, backward compatibility, and wiring
  - [x] 15.1 Write shadow sync integration tests
    - One integration test per direction over an emulated named shadow: edge report ingested through the IoT-rule/SQS path into the registry using the device's IoT identity; portal desired change delivered as a delta, applied, acknowledged, and marked synced, including the disconnected-then-reconnect pending-delivery case
    - _Requirements: 3.3, 5.5, 12.4_

  - [x] 15.2 Verify backward compatibility across all suites
    - Run every existing suite unchanged against the new build and assert baselines: LocalServer `test/backend-test` 204+3skip, portal backend 883, frontend vitest 423 + `npm run build`, infrastructure jest 30 + `npx tsc --noEmit`; assert existing Image_Source records, classic pipeline configurations, saved workflow definitions, and prior deployments behave identically
    - _Requirements: 11.1, 11.2, 11.3, 11.4, 11.5_

  - [x] 15.3 Final wiring and deployment readiness
    - Verify end-to-end wiring in code: `server_setup.py` starts discovery and the sync agent, CDK stack synthesizes with the new table/queue/Lambdas/routes/IoT rule, API routes registered and reachable in the moto-backed route tests, frontend views wired to the new endpoints; fix any gaps surfaced by the full test runs
    - _Requirements: 1.3, 3.2, 5.1, 8.6, 11.2_

- [x] 16. Final checkpoint
  - Ensure all tests pass across all four baselines, ask the user if questions arise.

## Notes

- Each task references specific requirements for traceability
- Property tests use hypothesis (Python) and fast-check (TypeScript) with a minimum of 100 iterations, tagged `**Feature: camera-registry-sync, Property {number}: {property_text}**`; all 22 design properties are covered by tasks 1.2, 1.3, 1.5, 2.2, 2.4, 2.7, 5.2, 5.3, 6.3, 6.4, 9.2, 10.2, 11.2–11.6, 11.8, 11.9, 14.2, 14.3, 14.5
- Pure cores are built first (discovery enumeration/diff, `build_inventory`, `reduce_report`, `validate_camera_bindings`, `resolve_bindings`) — everything else consumes them
- Portal backend tests run against the moto-backed conftest stack in `edge-cv-portal/backend/tests`; LocalServer tests run with `PYTHONPATH=src/backend:test/backend-test`
- V4L2 ioctls, IoT shadow transport, and Greengrass delivery are exercised through injectable fakes in unit/property tests; single integration examples cover the AWS-owned transports (task 15.1)
- No SQLite schema changes, no accessor changes, no packaged-artifact changes: all edge access goes through existing accessors, and bindings travel in the `dda-camera-bindings` shadow

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "4.1", "5.1", "10.1"] },
    { "id": 1, "tasks": ["1.2", "1.4", "4.2", "5.2", "10.2", "11.1"] },
    { "id": 2, "tasks": ["1.3", "1.5", "2.1", "5.3", "10.3", "11.2"] },
    { "id": 3, "tasks": ["1.6", "2.2", "5.4", "6.1", "11.3"] },
    { "id": 4, "tasks": ["2.3", "5.5", "6.2", "9.1", "11.4"] },
    { "id": 5, "tasks": ["2.4", "6.3", "8.1", "9.2", "11.5", "11.7"] },
    { "id": 6, "tasks": ["2.5", "2.6", "6.4", "9.3", "11.6", "12.1"] },
    { "id": 7, "tasks": ["2.7", "6.5", "8.2", "11.8", "14.1"] },
    { "id": 8, "tasks": ["2.8", "6.6", "11.9", "12.2", "14.2"] },
    { "id": 9, "tasks": ["2.9", "11.10", "14.3", "14.4"] },
    { "id": 10, "tasks": ["14.5", "15.1"] },
    { "id": 11, "tasks": ["14.6", "15.2"] },
    { "id": 12, "tasks": ["15.3"] }
  ]
}
```
