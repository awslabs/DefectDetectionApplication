# Implementation Plan: Quality Station HMI

## Overview

Implementation proceeds backend-first (the two small additive LocalServer changes), then builds the framework-free TypeScript SPA in a new top-level `hmi/` directory from the inside out: pure logic modules with their fast-check property tests first, then the app reducer, then the effectful shells (API client, poller, renderer), and finally wiring, layout tests, and the serving smoke check. Each of the design's 15 correctness properties is a separate property-test sub-task placed immediately after the module it validates.

## Tasks

- [x] 1. Backend: additive recent-executions route and /hmi static mount
  - [x] 1.1 Implement bounded recent-executions route in `src/backend/workflow_engine/api.py`
    - Add `GET /workflows/registrations/{registration_id}/executions?limit=N` on the existing authenticated router
    - Return executions of that registration only, ordered `started_at` DESC with `id` DESC as tiebreak, bounded by `limit` (default 10, clamped 1..50)
    - Reuse `execution_to_dict` for the response shape; return 404 for an unknown registration; change no existing route or response shape
    - _Requirements: 3.6_

  - [ ]* 1.2 Write property test for the recent-executions route
    - **Property 8: Recent-executions endpoint is bounded and ordered**
    - Hypothesis test in `test/backend-test/workflow_engine/test_registration_executions_api.py`, following the standalone-app + in-memory DB pattern of `test_workflow_run_results_api.py` (run with `PYTHONPATH=src/backend`)
    - Generate arbitrary sets of stored WorkflowExecutions across registrations and arbitrary `limit` values; assert registration filtering, `clamp(limit, 1, 50)` bound, and `started_at` DESC / `id` DESC ordering
    - **Validates: Requirements 3.6**

  - [ ]* 1.3 Write example tests for the recent-executions route
    - 404 for an unknown registration; default limit of 10; clamping of out-of-range limit values (0, negative, > 50)
    - Add to `test/backend-test/workflow_engine/test_registration_executions_api.py`
    - _Requirements: 3.6_

  - [x] 1.4 Add guarded /hmi static mount in `src/backend/app.py`
    - Mount `StaticFiles(directory=HMI_DIST_DIR, html=True)` at `/hmi`, guarded by directory existence so devices without the bundle behave identically to today
    - Serve the HMI same-origin with the API; no existing route changes
    - _Requirements: 6.7_

- [x] 2. Checkpoint - Ensure backend tests pass
  - Run the backend workflow_engine test suite (`PYTHONPATH=src/backend pytest test/backend-test/workflow_engine/`); ensure all tests pass, ask the user if questions arise.

- [x] 3. Frontend scaffold
  - [x] 3.1 Scaffold the `hmi/` project (Vite + TypeScript + Vitest + fast-check)
    - Create top-level `hmi/` with `package.json` (pinned versions), `tsconfig.json`, `vite.config.ts` (base `/hmi/`, static-asset output), `vitest` config, `index.html`, and the module directory layout from the design (`src/auth`, `src/api`, `src/logic`, `src/app`, `src/ui`)
    - Add fast-check and configure a minimum of 100 iterations per property test
    - _Requirements: 6.7_

- [x] 4. API types, URL builders, and formatting
  - [x] 4.1 Implement API data models and defensive parse functions
    - `hmi/src/api/types.ts`: `Registration`, `Execution`, `ResultImage`, `RunMetadata` mirrors plus narrow parse functions (unknown fields ignored, missing fields defaulted) per the design's defensive boundary
    - _Requirements: 2.6, 4.7_

  - [x] 4.2 Implement URL builders in `hmi/src/api/routes.ts`
    - Pure builders for all consumed routes; image URLs (`/output-image`, `/node-image?nodeId=&port=`) carry the Session_Token as an encoded `token` query parameter
    - _Requirements: 1.3, 5.5_

  - [ ]* 4.3 Write property test for image URL construction
    - **Property 3: Image URLs carry the token in query**
    - fast-check over arbitrary tokens, executionIds, nodeIds, and ports
    - **Validates: Requirements 1.3, 5.5**

  - [x] 4.4 Implement timestamp formatting in `hmi/src/logic/format.ts`
    - Epoch-seconds → local-time-zone display strings with at least seconds precision
    - _Requirements: 4.6_

  - [ ]* 4.5 Write property test for timestamp formatting
    - **Property 10: Timestamp formatting**
    - fast-check over arbitrary epoch-seconds timestamps
    - **Validates: Requirements 4.6**

- [x] 5. Authentication and API client
  - [x] 5.1 Implement session management in `hmi/src/auth/session.ts`
    - Startup screen decision (login iff no stored token or `expiresAt <= now`); token + `expiresAt` persistence in `localStorage["hmi.session"]`; in-memory-only credential retention after successful login
    - _Requirements: 1.1, 1.2, 1.5_

  - [ ]* 5.2 Write property test for the startup session decision
    - **Property 1: Startup session decision**
    - fast-check over arbitrary stored session states (absent, or any `expiresAt`) and current times
    - **Validates: Requirements 1.1, 1.5**

  - [x] 5.3 Implement `apiFetch` wrapper in `hmi/src/api/client.ts`
    - Attach `Authorization: Bearer <token>`; 10-second timeout via `AbortController`; error classification (`network` / `timeout` / `http-5xx` / `http-401` / `http-other`)
    - On 401 from any route except login: single re-login with in-memory credentials guarded by a module-level in-flight latch, single retry of the original request; on failure or missing credentials, discard the stored token and surface the login screen
    - Login-response handling: 403 → local-login-disabled state; 401 → credentials-rejected state with nothing stored
    - _Requirements: 1.2, 1.4, 1.6, 1.7, 1.8_

  - [ ]* 5.4 Write property test for 401 re-login behavior
    - **Property 2: Single re-login on 401**
    - fast-check over scripted response sequences containing 401s; assert at most one login and one retry per 401, token discarded and login screen on re-login failure or missing credentials
    - **Validates: Requirements 1.4, 1.8**

  - [ ]* 5.5 Write example tests for auth wiring
    - Login success stores token + subsequent requests carry the bearer header (1.2); 403 → "local login is disabled" message (1.6); 401 → credentials-rejected message, nothing stored, form retained (1.7)
    - _Requirements: 1.2, 1.6, 1.7_

- [ ] 6. Pure display logic modules
  - [x] 6.1 Implement run ordering logic in `hmi/src/logic/runs.ts`
    - Terminal-run comparator (`finishedAt` DESC, `startedAt` tiebreak when `finishedAt` values are equal or absent), latest-terminal selection, in-progress (`pending`/`running`) detection
    - _Requirements: 3.2, 3.4, 3.7_

  - [x] 6.2 Implement registration filtering and selection in `hmi/src/logic/selection.ts`
    - Active (`registered`) status filter; labeling by `name` when present and non-empty, else `workflowId`; default selection by latest most-recent-run `startedAt`, falling back to first active registration when no runs exist; availability check for the displayed registration (absent/non-active → unavailable, remaining actives offered, zero actives → no-workflows)
    - _Requirements: 2.2, 2.4, 2.5, 2.6, 2.7, 8.5_

  - [ ]* 6.3 Write property test for registration filtering and labeling
    - **Property 4: Registration filtering and labeling**
    - fast-check over registration lists with arbitrary statuses and names (including null/empty names)
    - **Validates: Requirements 2.2**

  - [ ]* 6.4 Write property test for default workflow selection
    - **Property 5: Default workflow selection**
    - fast-check over active registrations with arbitrary run lists
    - **Validates: Requirements 2.4, 2.7**

  - [x] 6.5 Implement image-pair selection in `hmi/src/logic/images.ts`
    - `selectImagePair(images)` per the design: first `reference`-port node entry; captured = same-node `in` entry, else first `in` in list order, else `output` entry, else none; single-node pairing; `hasMoreNodes` flag; no-reference → single-panel layout
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.7_

  - [ ]* 6.6 Write property test for image-pair selection
    - **Property 11: Deterministic image-pair selection**
    - fast-check over arbitrary results `images` lists (mixed kinds, ports, nodeIds, orders)
    - **Validates: Requirements 5.1, 5.2, 5.3, 5.4, 5.7**

  - [x] 6.7 Implement verdict derivation in `hmi/src/logic/verdict.ts`
    - Metadata + execution status → `VerdictViewModel`: pass/fail from `is_anomalous`, confidence rounded to ≤ 2 decimals, `generated_text` truncated at 500 chars with truncation flag, no-verdict for verdict-less completed runs, failed-run state with error summary or no-details message, metadata-unavailable indication
    - _Requirements: 4.2, 4.3, 4.4, 4.5, 4.7, 4.8, 4.9_

  - [ ]* 6.8 Write property test for verdict derivation
    - **Property 9: Verdict view-model derivation**
    - fast-check over arbitrary execution statuses and metadata objects (missing/extra fields, boundary text lengths)
    - **Validates: Requirements 4.2, 4.3, 4.4, 4.5, 4.7, 4.8**

  - [x] 6.9 Implement history strip logic in `hmi/src/logic/history.ts`
    - Build initial history from existing runs (newest first, capacity 10); insert new terminal runs at the newest position; evict exactly the oldest on overflow; entries carry verdict state and start time; fewer-than-capacity and zero-run cases
    - _Requirements: 7.1, 7.2, 7.6, 7.7_

  - [ ]* 6.10 Write property test for history strip invariants
    - **Property 12: History strip invariants**
    - fast-check over arbitrary initial run lists and sequences of terminal-run insertions
    - **Validates: Requirements 7.1, 7.2, 7.6, 7.7**

- [x] 7. Checkpoint - Ensure all frontend logic tests pass
  - Run `npx vitest --run` in `hmi/`; ensure all tests pass, ask the user if questions arise.

- [ ] 8. App state machine (reducer)
  - [x] 8.1 Implement auth and connection slices of the reducer in `hmi/src/app/machine.ts`
    - `(AppState, Event) → AppState` transitions for AUTH/CONNECTED/DISCONNECTED per the design state diagram: disconnect exactly on network error / 10 s timeout / HTTP 5xx (401 routes to auth), retain last Run_Result and last-successful-update time, reconnect on any successful response with the update cycle resumed
    - _Requirements: 8.1, 8.3, 8.4_

  - [ ]* 8.2 Write property test for connection state transitions
    - **Property 14: Connection state transitions**
    - fast-check over arbitrary sequences of request outcomes
    - **Validates: Requirements 8.1, 8.3**

  - [x] 8.3 Implement live-view run selection and in-progress handling in the reducer
    - Poll-payload event handling: displayed run becomes the maximal terminal run (using `logic/runs.ts` ordering) in a single reducer step; in-progress indicator on iff a `pending`/`running` execution exists, never disturbing the displayed Run_Result; no-runs message state with the cycle continuing
    - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.7, 3.8, 2.8_

  - [ ]* 8.4 Write property test for maximal terminal run display
    - **Property 6: Live view displays the maximal terminal run**
    - fast-check over arbitrary prior live-mode states and polled execution lists
    - **Validates: Requirements 3.1, 3.2, 3.4, 3.7**

  - [ ]* 8.5 Write property test for non-destructive in-progress detection
    - **Property 7: In-progress detection is non-destructive**
    - fast-check over arbitrary app states and polled execution lists
    - **Validates: Requirements 3.3**

  - [x] 8.6 Implement historical mode and workflow availability in the reducer
    - History-run selection pins the displayed Run_Result with the historical indicator; new terminal runs update history and set `newerRunAvailable` without replacing the view; return-to-live resumes live mode and maximal-terminal display; registrations-payload handling drives unavailable/no-workflows messages and offered alternatives (using `logic/selection.ts`)
    - _Requirements: 7.3, 7.4, 7.5, 8.5_

  - [ ]* 8.7 Write property test for historical mode pinning
    - **Property 13: Historical mode pinning and return-to-live round trip**
    - fast-check over arbitrary sequences of new-terminal-run events during historical mode, followed by return-to-live
    - **Validates: Requirements 7.4, 7.5**

  - [ ]* 8.8 Write property test for workflow availability handling
    - **Property 15: Workflow availability handling**
    - fast-check over arbitrary registrations payloads and displayed registrationIds
    - **Validates: Requirements 8.5**

- [ ] 9. Effectful shells and wiring
  - [x] 9.1 Implement the polling loop in `hmi/src/app/poller.ts`
    - 2-second executions poll (`GET .../executions?limit=10`) for the displayed registration while connected; fetch `/results` + `/metadata` (metadata retried once) only when the latest terminal run changes; per-execution LRU cache (capacity 20); every 15th cycle refresh `GET /workflows/registrations`; 10-second retry probe while disconnected; on reconnect, immediate poll + unconditional Live_View and history refresh; registrations fetched after login; selection swap triggers an immediate poll
    - _Requirements: 2.1, 2.3, 3.1, 3.6, 4.1, 4.9, 8.2, 8.6, 8.7_

  - [ ]* 9.2 Write example tests for poller and data wiring (fake timers)
    - 2 s cycle period (3.1); continued polling in empty states (3.8); 10 s disconnected retry cadence (8.2); connected steady state (8.4); reconnect refresh of Live_View and history including unchanged data (8.6, 8.7); selection swap within 2 s (2.3); registrations fetched after login (2.1); results+metadata fetched on completed run (4.1); metadata retry-once (4.9); historical-run fetch failure (7.8)
    - _Requirements: 2.1, 2.3, 3.1, 3.8, 4.1, 4.9, 7.8, 8.2, 8.4, 8.6, 8.7_

  - [x] 9.3 Implement DOM rendering and kiosk CSS in `hmi/src/ui/render.ts`
    - Render `AppState` into the three-band CSS Grid layout (72px header / main / 136px history strip; main `minmax(360px, 440px)` verdict column): login form, header (workflow name + run start time, connection badge, in-progress indicator), verdict panel (icon + word ≥ 48px via clamp, confidence, truncated text with indicator, finished time), side-by-side labeled image panels (`object-fit: contain`, equal heights, full-width captured frame without reference, more-nodes badge), history strip tiles with historical-mode banner and return-to-live control, empty/unavailable/error states
    - `<img>` onerror/timeout → per-panel "image unavailable" placeholder, never substituting another port or run
    - _Requirements: 2.8, 3.5, 4.2, 5.1, 5.4, 5.6, 6.1, 6.2, 6.3, 6.4, 6.5, 6.6, 6.8, 7.3, 7.7, 8.1_

  - [ ]* 9.4 Write example tests for rendering
    - Verdict states differ by icon and word, not color alone (4.2); image onerror → per-panel placeholder (5.6); empty states (2.8, 6.4, zero-history 7.7); historical-mode indicator and return control (7.3); automatic view replacement without interaction (3.5); header shows workflow name and start time (6.3)
    - _Requirements: 2.8, 3.5, 4.2, 5.6, 6.3, 6.4, 7.3, 7.7_

  - [x] 9.5 Wire the entry point in `hmi/src/main.ts`
    - Startup decision via `auth/session`, initial registrations fetch and default selection, reducer + poller + renderer subscription loop; produce the static bundle with `vite build`
    - _Requirements: 1.1, 1.5, 2.1, 2.4, 6.7_

- [ ] 10. Layout and serving verification
  - [ ]* 10.1 Write Playwright layout tests
    - Headless Chromium at 1920x1080: all primary Live_View content visible with no scroll overflow and no overlap (6.1); verdict computed font size ≥ 48 px in both states (6.2); equal image display heights with differing aspect ratios, uncropped (6.5); no horizontal overflow at 1280 and 1920 viewport widths (6.6)
    - _Requirements: 6.1, 6.2, 6.5, 6.6_

  - [ ]* 10.2 Write smoke test for the /hmi static mount
    - Backend test asserting the mount serves `index.html` when the dist directory exists and that the app starts unchanged (no mount) when it does not
    - _Requirements: 6.7_

- [x] 11. Final checkpoint - Ensure all tests pass
  - Run the backend suite (`PYTHONPATH=src/backend pytest test/backend-test/workflow_engine/`) and the frontend suite (`npx vitest --run` in `hmi/`); ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP
- Each task references specific requirements for traceability; each of the design's 15 correctness properties has its own property-test sub-task placed next to the module it validates (P1–P7, P9–P15 with fast-check ≥ 100 iterations; P8 with Hypothesis)
- Requirement 6.8 (kiosk-mode Chromium on the station) is verified manually on the device; Requirement 2.6 (no name-keyed logic) is enforced by review, with arbitrary names/ids in property generators as a backstop
- Checkpoints ensure incremental validation; the backend additions are independent of the frontend and are validated first

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.4", "3.1"] },
    { "id": 1, "tasks": ["1.2", "4.1"] },
    { "id": 2, "tasks": ["1.3", "4.2", "4.4", "5.1"] },
    { "id": 3, "tasks": ["4.3", "4.5", "5.2", "5.3", "6.1", "6.2", "6.5", "6.7", "6.9"] },
    { "id": 4, "tasks": ["5.4", "5.5", "6.3", "6.4", "6.6", "6.8", "6.10", "8.1"] },
    { "id": 5, "tasks": ["8.2", "8.3"] },
    { "id": 6, "tasks": ["8.4", "8.5", "8.6"] },
    { "id": 7, "tasks": ["8.7", "8.8", "9.1", "9.3"] },
    { "id": 8, "tasks": ["9.2", "9.4", "9.5"] },
    { "id": 9, "tasks": ["10.1", "10.2"] }
  ]
}
```
