# Implementation Plan: IMTS Triple Inspection HMI

## Overview

Two independent tracks, sequenced so they never block each other:

- **Backend track (task 1)**: one additive change in `src/backend/workflow_engine/output_bindings.py` (the `BedrockInferenceProcessor` crop path) that persists `{capture_id}.node.{sanitizedNodeId}.original.jpg` at crop time and `{capture_id}.node.{sanitizedNodeId}.annotated.jpg` after the Bedrock answer returns. Zero changes to `run_artifacts.py`, `api.py`, or `download_file.py` — `list_node_images` is filename-pattern keyed and port-generic. Both persists are best-effort in the `_persist_node_frames` containment style and must never affect run status or the existing `is_anomalous`/`confidence` parse and metadata merge.
- **Frontend track (tasks 3–13)**: a second Vite entry point inside the existing `hmi/` project (`hmi/triple.html` + `hmi/src/triple/`) built into the same `hmi/dist` and served by the existing `/hmi` static mount at `/hmi/triple.html`. The existing `hmi/src` modules (`auth/session.ts`, `api/client.ts`, `api/routes.ts`, `api/types.ts`, `logic/runs.ts`, `logic/format.ts`) are reused **unchanged**; no existing `hmi/src` module may change behavior, the existing `index.html` entry must still build, and the existing test suite must still pass.

Pure logic modules and their property tests land before the reducer, renderer, and poller that consume them.

## Tasks

- [x] 1. Backend: additive per-inspection run artifacts
  - [x] 1.1 Persist the Inspection's Original_Image at crop time
    - Add `_persist_original_frame(run_context, node_id, crop_bytes)` to `BedrockInferenceProcessor` in `src/backend/workflow_engine/output_bindings.py`, called from the `_detection_crop` success path beside the existing `CROP_ARTIFACT_TEMPLATE` persist
    - Write the exact crop bytes sent to Bedrock to `{output_dir}/{capture_id}.node.{sanitizedNodeId}.original.jpg`, sanitizing the node id with the executor's `_UNSAFE_NODE_ID_CHARS` discipline so the filename parses back to the same (`nodeId`, `port`) pair `list_node_images` reports
    - Entirely best-effort in the `_persist_node_frames` containment style: any failure logged and swallowed, run status and outcome untouched
    - No changes to `run_artifacts.py`, `api.py`, or `download_file.py`
    - _Requirements: 4.4_

  - [x] 1.2 Write property test for additive inventory listing and resolution
    - **Property 18: Additive inventory listing and resolution (backend)**
    - **Validates: Requirements 4.4**
    - Hypothesis, new file `test/backend-test/workflow_engine/test_property_triple_artifact_listing.py`, tmp-dir artifact fixture pattern from `test_workflow_run_results_api.py`, minimum 100 iterations
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 18: Additive inventory listing and resolution**`

  - [x] 1.3 Extract the answer's Defect_Objects and persist the Annotated_Image
    - Add a tolerant `objects`-list extractor in the `parse_bedrock_answer` style (first JSON object found, fenced code blocks and surrounding prose tolerated) plus `_persist_annotated_frame(run_context, node_id, crop_bytes, answer_text)`, called after the Bedrock invocation returns in `process`/`_run_one`, after the existing parse and metadata merge
    - Draw each valid Defect_Object's `bounding_box` (crop pixel space, clamped to the crop bounds) onto a copy of the crop bytes with `cv2` as a rectangle outline plus the object's `name`/`qc` label — red for NOK, green for OK; skip entries whose box is missing, malformed, or empty after clamping without affecting valid entries
    - Persist to `{output_dir}/{capture_id}.node.{sanitizedNodeId}.annotated.jpg`; persist nothing when the answer yields no parseable `objects` list; persist the crop unchanged with zero boxes for a parseable but empty `objects: []`
    - Do not merge the parsed `objects` into run metadata — the metadata shape stays byte-identical to today
    - Best-effort containment: any failure logged and swallowed, never affecting the run or the `is_anomalous`/`confidence` merge
    - _Requirements: 4.4, 4.12_

  - [x] 1.4 Write property test for annotated-frame rendering
    - **Property 19: Annotated frame renders exactly the answer's valid Defect_Object boxes (backend)**
    - **Validates: Requirements 4.4, 4.12**
    - Hypothesis, new file `test/backend-test/workflow_engine/test_property_triple_annotated_frame.py`, synthetic crop arrays plus generated answer strings (valid, invalid, and mixed `objects` entries; fenced, prose-wrapped, and objects-less variants), minimum 100 iterations
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 19: Annotated frame renders exactly the answer's valid Defect_Object boxes**`

  - [x] 1.5 Write containment and preservation unit tests for the additive persists
    - New file `test/backend-test/workflow_engine/test_triple_artifact_containment.py`: a raising `cv2`/filesystem stub in either persist leaves run status, node outcome, and the `bedrock.{nodeId}.*` + flat `is_anomalous`/`confidence` metadata identical to the pre-change path
    - Assert a binding without `crop_detection_index` produces no `original`/`annotated` artifacts (runs of other workflows stay byte-identical)
    - _Requirements: 4.4_

- [x] 2. Checkpoint - backend additive change complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 3. Frontend: second Vite entry point in the existing hmi project
  - [x] 3.1 Add the multi-page build and the triple entry scaffold
    - Extend `hmi/vite.config.ts` with a multi-page `build.rollupOptions.input` covering `index.html` and the new `triple.html`, keeping `base: "/hmi/"`, `outDir: dist`, and static-assets-only output
    - Create `hmi/triple.html`, `hmi/src/triple/main.ts` (entry placeholder), and the Kiosk_Display stylesheet with the CSS Grid shell (`grid-template-rows: 72px 1fr 140px`, main row `repeat(3, 1fr)` with fixed gap, image panels `aspect-ratio: 1 / 2` + `object-fit: contain`, verdict text sized via `clamp()` for a ≥32 px rendered height)
    - Change no existing `hmi/src` module and no backend serving code; the existing `index.html` entry must keep building unchanged
    - _Requirements: 6.1, 6.2, 6.5, 6.6, 6.7, 6.8_

  - [x] 3.2 Write build smoke test for the multi-entry output
    - New file `hmi/test/triple-build.test.ts`: the build emits both `dist/index.html` and `dist/triple.html`, output is static assets only (no server-side entry), and asset URLs resolve under `/hmi/`
    - Run the existing suite to confirm the no-behavior-change guarantee for the existing HMI bundle
    - _Requirements: 6.6, 6.7_

- [x] 4. Target workflow configuration and binding (pure)
  - [x] 4.1 Implement `hmi/src/triple/config.ts`
    - `resolveWorkflowName(queryValue, buildTimeValue)`: query parameter when non-blank, else `VITE_TRIPLE_WORKFLOW_NAME` when non-blank, else `"blue-plate-detection-guided-inspection"`; blank/whitespace-only values fall through
    - _Requirements: 2.5_

  - [x] 4.2 Write property test for workflow-name configuration resolution
    - **Property 5: Workflow-name configuration resolution**
    - **Validates: Requirements 2.5**
    - Vitest + fast-check, `hmi/src/triple/config.test.ts`, minimum 100 iterations
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 5: Workflow-name configuration resolution**`

  - [x] 4.3 Implement `hmi/src/triple/binding.ts`
    - `bindTargetWorkflow(registrations, targetName)`: candidates are active registrations (`registered`, the backend's `ACTIVE_STATUSES` semantics) whose `name` is a case-sensitive exact match; one candidate binds; several → most recent `registeredAt`, ties or missing values → first in payload order; zero → not-deployed
    - Pure and evaluated on every registrations payload, so deploy/undeploy/redeploy transitions all reduce to this function
    - _Requirements: 2.2, 2.3, 2.4, 2.7, 8.5, 8.8_

  - [x] 4.4 Write property test for target-workflow binding
    - **Property 4: Target-workflow binding is a deterministic pure function**
    - **Validates: Requirements 2.2, 2.3, 2.4, 2.7, 8.5, 8.8**
    - Vitest + fast-check, `hmi/src/triple/binding.test.ts`, minimum 100 iterations
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 4: Target-workflow binding is a deterministic pure function**`

- [x] 5. Inspection derivation and verdict mapping (pure)
  - [x] 5.1 Implement `hmi/src/triple/inspections.ts`
    - `deriveInspections(images)`: take `kind === "node"` entries, group by `nodeId`, sort groups by lexicographic ascending `nodeId` and entries within a group by lexicographic ascending `port`; pair `original` := the `original`-port entry falling back to the `in`-port entry, `annotated` := the `annotated`-port entry with no fallback
    - `assignSlots(inspections)`: slots 1..3 from the first three Inspections in sorted order; remaining slots carry the no-inspection-data placeholder; more than three sets the more-inspections indicator
    - Every image reference carries its own Inspection's `nodeId` and its own `port`, so substitution across inspections, ports, or runs is impossible by construction
    - _Requirements: 4.2, 4.3, 4.6, 4.7, 4.10, 4.11, 5.4_

  - [x] 5.2 Write property test for inspection derivation and slot stability
    - **Property 9: Deterministic inspection derivation and stable slot assignment**
    - **Validates: Requirements 4.2, 4.3, 4.10, 4.11, 5.4**
    - Vitest + fast-check, `hmi/src/triple/inspections.test.ts`, minimum 100 iterations
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 9: Deterministic inspection derivation and stable slot assignment**`

  - [x] 5.3 Write property test for slot-count clamping
    - **Property 10: Slot-count clamping**
    - **Validates: Requirements 4.6, 4.7**
    - Vitest + fast-check, `hmi/src/triple/inspections.slots.test.ts`, minimum 100 iterations
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 10: Slot-count clamping**`

  - [x] 5.4 Implement `hmi/src/triple/verdicts.ts`
    - `deriveVerdicts(status, metadata, inspections)` producing `InspectionSlotVM[]` and `RunResultVM` fields: per-slot verdicts only from `metadata.bedrock?.[nodeId]` (sanitized-form comparison for raw-vs-sanitized node ids), `is_anomalous === true` → FAIL, `=== false` → PASS, absent/non-boolean → NO VERDICT for that slot only
    - Run-level verdict only from the flat `is_anomalous`/`confidence`, rendered once at run level and never duplicated into slots; both sets render in their own positions when both exist
    - `confidence` rendered rounded to exactly 2 decimal places; completed run with no verdict fields yields images + status with no verdict content and no error state
    - Failed run yields the run-level failure state with the execution's `error` summary (fallback message when empty/absent), placeholders in all three slots, and no image reference from any prior run
    - Verdict states carry an icon + distinct word (✔ PASS / ✘ FAIL / — NO VERDICT / ⚠ ERROR), never color alone
    - _Requirements: 5.5, 5.6, 5.7, 5.9, 5.10, 5.11, 5.12_

  - [x] 5.5 Write property test for per-inspection verdict derivation
    - **Property 11: Per-inspection verdict derivation**
    - **Validates: Requirements 5.5, 5.7, 5.10, 5.12**
    - Vitest + fast-check, `hmi/src/triple/verdicts.test.ts`, minimum 100 iterations
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 11: Per-inspection verdict derivation**`

  - [x] 5.6 Write property test for verdict placement
    - **Property 12: Verdict placement without conflation**
    - **Validates: Requirements 5.6, 5.11**
    - Vitest + fast-check, `hmi/src/triple/verdicts.placement.test.ts`, minimum 100 iterations
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 12: Verdict placement without conflation**`

  - [x] 5.7 Write property test for the failed-run view model
    - **Property 13: Failed-run view model**
    - **Validates: Requirements 5.9**
    - Vitest + fast-check, `hmi/src/triple/verdicts.failed.test.ts`, minimum 100 iterations
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 13: Failed-run view model**`

- [x] 6. Run history logic (pure)
  - [x] 6.1 Implement `hmi/src/triple/history.ts`
    - `runVerdictState(execution, slots)` precedence: `failed-run` when the run failed, else `fail` when any Inspection fails, else `no-verdict` when any of the three lacks a boolean verdict, else `pass`
    - `buildHistory(executions, slotsByExecution)` and `insertHistoryEntry(history, entry)`: newest first, capacity 10 (≥ the required 5 visible), evicting exactly the oldest entry on overflow, containing only runs that exist
    - _Requirements: 7.1, 7.2, 7.6, 7.8_

  - [x] 6.2 Write property test for history invariants and verdict precedence
    - **Property 14: History invariants and verdict precedence**
    - **Validates: Requirements 7.1, 7.2, 7.6, 7.8**
    - Vitest + fast-check, `hmi/src/triple/history.test.ts`, minimum 100 iterations
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 14: History invariants and verdict precedence**`

- [x] 7. Checkpoint - pure logic modules complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 8. Triple app reducer
  - [x] 8.1 Implement `hmi/src/triple/machine.ts`
    - Define `TripleAppState` (auth, connection with `consecutivePollFailures`, binding, live with mode/displayed/inProgress/history/newerRunAvailable) and the event union; implement the pure reducer `(TripleAppState, Event) → TripleAppState`
    - Displayed run selection reuses `logic/runs.ts` terminal-run ordering (`finishedAt` desc, `startedAt` tiebreak); in-progress flag from pending/running executions without disturbing displayed content
    - Poll failures retain content and increment the counter; ≥5 consecutive failures raise the stale-data indicator, any success resets it
    - Connection transitions: network error / 10 s timeout / HTTP 5xx → disconnected with last Run_Result and last-successful-update retained; any 2xx while disconnected → connected; 401 routes to the auth path, never to disconnected
    - Binding events re-run `bindTargetWorkflow` on every registrations payload (not-deployed transition and automatic re-bind)
    - Historical mode pins the displayed run and sets `newerRunAvailable` on new terminal runs; return-to-live clears both and resumes live selection
    - _Requirements: 2.4, 2.6, 2.7, 3.2, 3.3, 3.4, 3.5, 3.7, 3.8, 3.9, 7.4, 7.5, 8.1, 8.3, 8.5, 8.8_

  - [x] 8.2 Write property test for poll-failure retention and staleness
    - **Property 8: Poll-failure retention and staleness accounting**
    - **Validates: Requirements 3.8, 3.9**
    - Vitest + fast-check, `hmi/src/triple/machine.stale.test.ts`, minimum 100 iterations
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 8: Poll-failure retention and staleness accounting**`

  - [x] 8.3 Write property test for historical pinning and return-to-live
    - **Property 15: Historical pinning and return-to-live round trip**
    - **Validates: Requirements 7.4, 7.5**
    - Vitest + fast-check, `hmi/src/triple/machine.historical.test.ts`, minimum 100 iterations
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 15: Historical pinning and return-to-live round trip**`

  - [x] 8.4 Extend the displayed-run property test to the triple reducer
    - **Property 6: Displayed run is the maximal terminal run**
    - **Validates: Requirements 3.2, 3.3, 3.5, 3.7**
    - Existing property test of the reused `logic/runs.ts` ordering; extend it to fold polled execution lists through the triple reducer in `hmi/src/triple/machine.displayed.test.ts`, minimum 100 iterations. One property, one test — do not duplicate the run-ordering assertions already covered
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 6: Displayed run is the maximal terminal run**`

  - [x] 8.5 Extend the in-progress property test to the triple reducer
    - **Property 7: In-progress indicator is accurate and non-destructive**
    - **Validates: Requirements 3.4**
    - Extend the existing coverage to the triple reducer in `hmi/src/triple/machine.inprogress.test.ts`, minimum 100 iterations
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 7: In-progress indicator is accurate and non-destructive**`

  - [x] 8.6 Extend the connection-state property test to the triple reducer
    - **Property 16: Connection state transitions**
    - **Validates: Requirements 8.1, 8.2, 8.3**
    - Extend the existing coverage to the triple connection machine in `hmi/src/triple/machine.connection.test.ts`, minimum 100 iterations
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 16: Connection state transitions**`

- [x] 9. Extend the reused shared-module property tests
  - [x] 9.1 Extend image-URL generators with the new ports
    - **Property 3: Image URLs carry the token in query**
    - **Validates: Requirements 1.3, 4.5**
    - Add `original` and `annotated` to the port generators in the existing `hmi/src/api/routes.test.ts` without changing `api/routes.ts`, minimum 100 iterations
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 3: Image URLs carry the token in query**`

  - [x] 9.2 Confirm the startup session-decision property covers the triple entry
    - **Property 1: Startup session decision**
    - **Validates: Requirements 1.1, 1.5**
    - Confirm the existing `hmi/src/auth/session.test.ts` property covers the session key the triple entry uses; extend generators only if the key namespace differs. No change to `auth/session.ts`
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 1: Startup session decision**`

  - [x] 9.3 Confirm the single-re-login property covers the triple call sites
    - **Property 2: Single re-login on 401**
    - **Validates: Requirements 1.4**
    - Confirm the existing `hmi/src/api/client.test.ts` property covers the routes the triple entry calls; extend the scripted-response generators only where new routes are added. No change to `api/client.ts`
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 2: Single re-login on 401**`

  - [x] 9.4 Confirm the timestamp-formatting property covers the triple header
    - **Property 17: Timestamp formatting**
    - **Validates: Requirements 6.3, 6.9**
    - Confirm the existing `hmi/src/logic/format.test.ts` property covers the run-timing rendering the triple header uses (local time, seconds precision, finish omitted when absent). No change to `logic/format.ts`
    - Tag: `**Feature: imts-triple-inspection-hmi, Property 17: Timestamp formatting**`

- [x] 10. Kiosk rendering
  - [x] 10.1 Implement `hmi/src/triple/render.ts` three-slot layout rendering
    - Render header (workflow name, run timing via `logic/format.ts`, run-level verdict, in-progress / connection / stale indicators), the three Inspection_Slots (slot identifier, per-slot verdict, labeled ANNOTATED and ORIGINAL panels side by side at equal heights, aspect preserved, uncropped), and the history strip (newest first, clickable tiles, historical indicator and return-to-live control)
    - Render every empty and error state from the view models: no runs recorded, no terminal runs, no-inspection-data slots, more-inspections indicator, verdict-data-unavailable, inspection-data-unavailable, failed-run summary, zero-history message, historical-fetch error
    - _Requirements: 2.6, 3.4, 3.6, 3.7, 3.9, 4.6, 4.7, 4.8, 4.9, 5.1, 5.2, 5.3, 5.4, 5.5, 5.9, 6.1, 6.3, 6.4, 6.9, 7.1, 7.3, 7.6, 7.7, 8.1, 8.4_

  - [x] 10.2 Implement per-panel image loading with placeholders
    - Build each `<img>` URL from the displayed run's own `executionId` and the panel's own (`nodeId`, `port`) via the existing `nodeImageUrl` builder with the Session_Token in the `token` query parameter; apply a 10 s per-image timeout
    - On error or timeout render a placeholder in that panel only, never substituting an image from a different Inspection, port, or run; render the no-annotated-image placeholder when the Inspection has no `annotated` entry
    - _Requirements: 4.5, 4.10, 4.11, 5.8_

  - [x] 10.3 Write rendering unit tests
    - `hmi/src/triple/render.test.ts` (jsdom environment): three slots present with labeled annotated/original panels (5.1, 5.2); automatic slot replacement on a new run (3.6); verdict states differ by icon + word (5.5); `img` error → placeholder in that panel only (4.11, 5.8); empty states — no runs recorded (2.6), no terminal runs (3.7), zero history (7.6); historical indicator, return control, and selection rendering (7.3); historical fetch failure (7.7); header contents (6.3)
    - _Requirements: 2.6, 3.6, 3.7, 4.11, 5.1, 5.2, 5.5, 5.8, 6.3, 7.3, 7.6, 7.7_

- [x] 11. Poller and entry wiring
  - [x] 11.1 Implement `hmi/src/triple/poller.ts`
    - 2 s poll of `GET /workflows/registrations/{id}/executions?limit=10`; fetch `/results` + `/metadata` (each retried once) only when the latest terminal run changes; periodic (~every 15th cycle) registrations refresh re-run through `bindTargetWorkflow`
    - 10 s unlimited disconnected retry probes against `GET /workflows/registrations`; any 2xx probe reconnects within 1 s and triggers an immediate poll plus an unconditional Live_View and history refresh
    - Dispatch all outcomes into the `triple/machine.ts` reducer; reuse `api/client.ts` unchanged for timeout, 5xx, and 401 handling
    - _Requirements: 2.1, 2.4, 3.1, 3.8, 4.1, 4.8, 4.9, 8.2, 8.3, 8.6, 8.7, 8.8_

  - [x] 11.2 Write poller unit tests with fake timers
    - `hmi/src/triple/poller.test.ts`: 2 s poll cadence (3.1); registrations fetched after token (2.1); not-deployed re-check cadence and automatic re-bind (2.4, 8.8); 10 s disconnected retry cadence (8.2); connected steady state (8.4); reconnect → immediate unconditional Live_View + history refresh (8.6, 8.7); results + metadata fetched on a new terminal run (4.1); retry-once wiring for both (4.8, 4.9); image-request 10 s timeout (4.5)
    - _Requirements: 2.1, 2.4, 3.1, 4.1, 4.5, 4.8, 4.9, 8.2, 8.4, 8.6, 8.7, 8.8_

  - [x] 11.3 Wire `hmi/src/triple/main.ts` and `hmi/triple.html`
    - Startup: `GET /local-auth/status` → login form or resume from the stored session; on token, resolve the workflow name via `config.ts`, fetch registrations, bind, start the poller, and render through `render.ts`
    - Wire history-tile selection, the return-to-live control, and the not-deployed / login-error screens; reuse `auth/session.ts` and `api/client.ts` unchanged
    - _Requirements: 1.1, 1.2, 1.3, 1.5, 1.8, 2.1, 2.4, 2.6, 7.3, 7.5_

  - [x] 11.4 Write auth wiring unit tests
    - `hmi/src/triple/main.test.ts`: login success stores the token and attaches the bearer header (1.2); 403 disabled, 401 rejected, and unreachable messages (1.6, 1.7, 1.9); startup `GET /local-auth/status` disabled → no form (1.8); stored unexpired token resumes without prompting (1.5)
    - _Requirements: 1.2, 1.5, 1.6, 1.7, 1.8, 1.9_

- [x] 12. Checkpoint - frontend wired end to end
  - Ensure all tests pass, ask the user if questions arise.

- [x] 13. Layout and artifact verification
  - [x] 13.1 Write Playwright layout tests at fixed viewports
    - Add Playwright (headless Chromium) as a dev dependency with its config and a `hmi/test/triple-layout.spec.ts` suite serving the built `triple.html` with stubbed API responses
    - Assert: all primary content visible without scrolling or overlap at 1920x1080 (6.1); slot widths equal within 2 px and image-panel aspect between 1:1.8 and 1:2.2 (6.2); equal image heights, aspect preserved, uncropped, ≥280 px wide at 1920 (5.3, 6.8); verdict rendered text height ≥32 px in every state (6.4); no horizontal overflow at 1280 and 1920 widths (6.5)
    - _Requirements: 5.3, 6.1, 6.2, 6.4, 6.5, 6.8_

  - [x] 13.2 Write an end-to-end artifact serving integration test
    - New file `test/backend-test/workflow_engine/test_triple_node_image_serving.py`: seed a run artifact directory with three node ids x `original`/`annotated` frames, then assert through the LocalServer test client that `GET /workflows/executions/{id}/results` reports exactly six additive node entries and that each (`nodeId`, `port`) pair is servable via `GET .../node-image`, with every pre-existing entry, field, and ordering unchanged
    - _Requirements: 4.4_

- [x] 14. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for a faster MVP.
- Task 1 (backend) and tasks 3–13 (frontend) have no cross-dependencies and can proceed in parallel.
- Properties 1–3, 6, 7, 16, and 17 already exist as passing tests of the reused `hmi/src` modules; tasks 8.4–8.6 and 9.1–9.4 extend that existing coverage rather than adding a second test per property — each correctness property stays implemented by exactly one property-based test.
- Every property test runs a minimum of 100 iterations (fast-check on the frontend via `hmi/test/setup.ts`, Hypothesis on the backend) and carries the `**Feature: imts-triple-inspection-hmi, Property {N}: {title}**` tag comment.
- Manual kiosk verification on the station's Chromium (6.7) and the one-off device run of the real blue-plate workflow are operational checks outside the coding task list; task 13.2 covers the same artifact contract automatically.
- No existing `hmi/src` module may change behavior, and no backend serving code (`run_artifacts.py`, `api.py`, `download_file.py`) may change.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "3.1", "4.1"] },
    { "id": 1, "tasks": ["1.3", "3.2", "4.2", "4.3", "5.1", "6.1"] },
    { "id": 2, "tasks": ["1.2", "1.4", "4.4", "5.2", "5.3", "5.4", "6.2"] },
    { "id": 3, "tasks": ["1.5", "5.5", "5.6", "5.7", "8.1", "9.1", "9.2", "9.3", "9.4"] },
    { "id": 4, "tasks": ["8.2", "8.3", "8.4", "8.5", "8.6", "10.1"] },
    { "id": 5, "tasks": ["10.2", "11.1"] },
    { "id": 6, "tasks": ["10.3", "11.3"] },
    { "id": 7, "tasks": ["11.2", "11.4", "13.1", "13.2"] }
  ]
}
```
