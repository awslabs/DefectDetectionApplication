# Implementation Plan: Concurrent Camera Stream Viewing

## Overview

This plan converts the broadcast-model design into incremental coding steps. It builds the shared
stream stack bottom-up: data models and configuration, the `CameraBackend` abstraction, the
single-slot `StreamSession`, the acquisition worker, then the process-wide `StreamBroadcaster`
registry. From there it rewires inference/capture and edit-settings onto the single shared claim,
exposes the new `/streams/...` API endpoints, updates the polling frontend preview, and closes with
real-device integration/performance tests.

Implementation language is **Python** (per the design's code), with **Hypothesis** property-based
tests added to the existing pytest suite under `test/backend-test`. New backend code lives under
`src/backend/utils/streaming/`. Each correctness property (1–21) maps to exactly one property-based
test placed close to the code it validates; stateful/interleaving properties (1, 2, 6, 8) use
Hypothesis `RuleBasedStateMachine`. All property tests run a minimum of 100 iterations against a
mock `CameraBackend` with an injected clock, parametrized over mock Aravis and GStreamer backends
where backend parity matters.

## Tasks

- [x] 1. Stream data models and configuration
  - [x] 1.1 Implement core stream data models
    - In `src/backend/utils/streaming/models.py`, define `LatestFrame` (frozen: data, width, height, seq, acquired_at), `Viewer` (viewer_id, camera_id, subscribed_at, last_active), `SessionState`, `FrameStatus`, `FrameResult`, and `SubscribeResult` dataclasses/enums
    - Add `src/backend/utils/streaming/__init__.py`
    - _Requirements: 2.2, 8.5_

  - [x] 1.2 Implement `StreamConfig` with timeout clamping
    - Add `StreamConfig` to `models.py` with frame_timeout_ms, open_timeout_ms, max_open_attempts, max_viewers, heartbeat/stale/first-frame timeouts, stale_after_s, min_refresh_fps
    - Clamp `frame_timeout_ms` and `open_timeout_ms` to [500, 30000] on construction
    - _Requirements: 7.1, 7.6_

  - [x]* 1.3 Write property test for timeout clamping
    - **Property 15: Timeout/configuration clamping** — effective timeout equals the configured value clamped to [500, 30000] ms
    - **Validates: Requirements 7.1**
    - Place in `test/backend-test/utils/streaming/test_stream_models.py`

- [x] 2. CameraBackend abstraction and adapters
  - [x] 2.1 Define the `CameraBackend` protocol and `RawFrame`
    - In `src/backend/utils/streaming/backends.py`, define the `CameraBackend` Protocol (open, start_stream, grab, apply_features, stop_stream, close) and the `RawFrame` type
    - _Requirements: 2.8_

  - [x] 2.2 Implement `AravisBackend` adapter
    - Wrap the existing `Camera` class / `get_camera_frame()` Aravis path; drive continuous acquisition from a worker loop instead of per-request software-trigger; route `apply_features` through `apply_device_features` / `_apply_config_features`
    - Enforce open with ≤ 3 attempts within open timeout
    - _Requirements: 2.8, 7.6_

  - [x] 2.3 Implement `GStreamerBackend` adapter
    - Wrap a persistent pipeline ending in an `appsink` pull loop (replacing one-shot `execute_image_source_pipeline` for live view); implement open/start_stream/grab/stop_stream/close
    - _Requirements: 2.8_

  - [x]* 2.4 Implement mock `CameraBackend` test double
    - In `test/backend-test/utils/streaming/mock_camera_backend.py`, record open/close/grab counts, simulate open failure, grab timeout/failure, and stop failure; provide mock Aravis and GStreamer variants and an injectable clock helper
    - _Requirements: 2.8_

  - [x]* 2.5 Write unit tests for backend adapters
    - Subscribe→grab→close happy path for each adapter against fakes
    - _Requirements: 2.8_

- [x] 3. StreamSession: latest-frame slot, viewer registry, freshness
  - [x] 3.1 Implement `StreamSession`
    - In `src/backend/utils/streaming/session.py`, implement single-writer/many-reader latest-frame slot (`publish`/`read_latest` under a brief writer lock), viewer registry (`add_viewer`/`remove_viewer`/`is_empty`), monotonic seq + acquired_at, and freshness/staleness + no-frame classification in `read_latest`
    - _Requirements: 2.2, 2.4, 2.5, 2.6, 4.6, 4.7_

  - [x]* 3.2 Write property test for fan-out
    - **Property 3: Fan-out delivers the identical latest frame to every viewer** — every viewer reading in an interval gets the byte-identical, same-seq, most-recent frame
    - **Validates: Requirements 1.1, 1.4, 2.2, 2.5**
    - `test/backend-test/utils/streaming/test_session_fanout.py`

  - [x]* 3.3 Write property test for independent, no-re-grab reads
    - **Property 4: Reads are independent of other viewers and do not re-grab** — get_frame is a pure function of the slot; repeated reads with no publish return the same frame and trigger no device grab
    - **Validates: Requirements 1.5, 2.4**
    - `test/backend-test/utils/streaming/test_session_reads.py`

  - [x]* 3.4 Write property test for frame freshness predicate
    - **Property 11: Frame freshness predicate** — read returns OK when `now - acquired_at <= stale_after_s`, STALE otherwise
    - **Validates: Requirements 4.6, 4.7**
    - `test/backend-test/utils/streaming/test_session_freshness.py`

  - [x]* 3.5 Write property test for no-frame-yet handling
    - **Property 12: No-frame-yet handling** — started session with no publish returns NO_FRAME and stays active
    - **Validates: Requirements 2.6, 4.2**
    - `test/backend-test/utils/streaming/test_session_noframe.py`

  - [x]* 3.6 Write property test for heartbeat/staleness predicate
    - **Property 9: Heartbeat update and staleness predicate** — heartbeat sets last_active to t; viewer is stale exactly when `now - last_active > stale_timeout_s`
    - **Validates: Requirements 3.9, 8.3**
    - `test/backend-test/utils/streaming/test_session_heartbeat.py`

- [x] 4. Acquisition worker
  - [x] 4.1 Implement the acquisition worker loop
    - In `src/backend/utils/streaming/worker.py`, implement one worker per session: grab→publish loop, rate ceiling cap that never drops below device cadence, first-frame timeout, and disconnect detection on grab timeout/failure (mark disconnected, transition session to ERROR→stop); retain last good slot on a failed grab
    - _Requirements: 2.7, 3.10, 3.11, 4.3, 4.4, 7.1_

  - [x]* 4.2 Write property test for producer cadence independence
    - **Property 5: Producer cadence is independent of viewer count** — published frame sequence is identical for 1 vs max_viewers readers; no read delays/prevents a publish (drop-to-latest)
    - **Validates: Requirements 4.5**
    - `test/backend-test/utils/streaming/test_worker_cadence.py`

  - [x]* 4.3 Write property test for acquisition-failure frame retention
    - **Property 13: Acquisition failure preserves the last good frame** — a failed grab does not overwrite the slot and an acquisition-failure indication is produced
    - **Validates: Requirements 2.7**
    - `test/backend-test/utils/streaming/test_worker_failure.py`

- [x] 5. StreamBroadcaster registry and lifecycle
  - [x] 5.1 Implement broadcaster registry, subscribe/unsubscribe, and session start/stop
    - In `src/backend/utils/streaming/broadcaster.py`, implement the process-wide `{camera_id -> StreamSession}` singleton with thread-safe subscribe (start-on-first), unsubscribe (stop-on-last with backend `close()`), `viewer_count`, and the single-device-claim invariant
    - _Requirements: 1.2, 2.1, 3.1, 3.3, 3.4, 3.7, 8.1, 8.4, 8.7, 8.8_

  - [x] 5.2 Implement viewer-limit and open-failure handling
    - Reject subscribe with reason `viewer_limit` at max_viewers (existing viewers untouched); on `open()` failure retry ≤ 3 attempts then reject with reason `camera_unavailable`, leaving no session/claim and not affecting other cameras
    - _Requirements: 1.3, 1.6, 1.7, 3.2, 7.6_

  - [x] 5.3 Implement heartbeat refresh and stale-viewer sweep
    - Implement `heartbeat` (refresh last_active), and `sweep_stale_viewers(now)` that deregisters viewers past stale_timeout_s, decrements counts, and stops emptied sessions
    - _Requirements: 3.8, 8.2, 8.3, 8.6, 8.7_

  - [x] 5.4 Implement `get_frame` state encoding and disconnection cascade
    - Encode NO_FRAME/STALE/DISCONNECTED in `get_frame` (refreshing heartbeat); on disconnect stop the session, release the claim before reporting completion, and return DISCONNECTED with no cached payload to all viewers
    - _Requirements: 2.6, 3.5, 3.11, 4.7, 7.2, 7.3, 7.4, 7.5_

  - [x]* 5.5 Write stateful property test for the single-claim invariant
    - **Property 1: Single device-claim invariant** — `RuleBasedStateMachine` over subscribe/unsubscribe/sweep/disconnect: claim count is 1 with ≥1 viewer, 0 with none, never > 1, never overlapping
    - **Validates: Requirements 1.2, 2.1, 3.4, 7.7**
    - `test/backend-test/utils/streaming/test_broadcaster_claim.py`

  - [x]* 5.6 Write stateful property test for session lifecycle
    - **Property 2: Session lifecycle (start on first, stop and release on last)** — `RuleBasedStateMachine`: first subscriber creates exactly one session; count returning to 0 stops it and invokes `close()`
    - **Validates: Requirements 3.1, 3.3, 3.7, 8.7**
    - `test/backend-test/utils/streaming/test_broadcaster_lifecycle.py`

  - [x]* 5.7 Write stateful property test for viewer-limit enforcement
    - **Property 6: Viewer-limit enforcement** — `RuleBasedStateMachine`: subscribe past max_viewers rejected with `viewer_limit`, count stays max, existing viewers still read
    - **Validates: Requirements 1.3, 1.6**
    - `test/backend-test/utils/streaming/test_broadcaster_viewerlimit.py`

  - [x]* 5.8 Write stateful property test for viewer-count and duplicate subscriptions
    - **Property 8: Viewer-count correctness and duplicate subscriptions** — `RuleBasedStateMachine`: reported count is a non-negative integer equal to distinct viewers; K subscribes yield K distinct ids each counted
    - **Validates: Requirements 8.1, 8.4, 8.5, 8.8**
    - `test/backend-test/utils/streaming/test_broadcaster_viewercount.py`

  - [x]* 5.9 Write property test for open-failure handling
    - **Property 7: Open-failure handling** — ≤ 3 open attempts then reject with `camera_unavailable`, no session/zero claims, other healthy cameras still subscribe
    - **Validates: Requirements 1.7, 3.2, 7.6**
    - `test/backend-test/utils/streaming/test_broadcaster_openfail.py`

  - [x]* 5.10 Write property test for stale sweep
    - **Property 10: Stale sweep deregisters, heartbeats keep alive** — sweep removes exactly viewers with `now - last_active > stale_timeout_s`; recently-heartbeated viewers retained
    - **Validates: Requirements 3.8, 8.2, 8.6**
    - `test/backend-test/utils/streaming/test_broadcaster_sweep.py`

  - [x]* 5.11 Write property test for the disconnection cascade
    - **Property 14: Disconnection cascade** — on grab timeout/failure marking disconnect, session stops with `close()` before completion and every viewer's subsequent get_frame returns DISCONNECTED with no payload
    - **Validates: Requirements 3.5, 3.11, 7.1, 7.2, 7.3, 7.4, 7.5**
    - `test/backend-test/utils/streaming/test_broadcaster_disconnect.py`

- [x] 6. Checkpoint - broadcaster core
  - Ensure all tests pass, ask the user if questions arise.

- [x] 7. Inference/capture single-claim sharing
  - [x] 7.1 Implement `get_inference_frame` reuse with dedicated-claim fallback
    - Add `get_inference_frame(camera_id, config)` to `broadcaster.py`: when a session is active return the session frame without opening a second claim; when none exists open exactly one dedicated claim, grab, and release it. On failure with an active session, return an error and leave the session + claim intact
    - _Requirements: 6.1, 6.3, 6.4, 6.6_

  - [x] 7.2 Rewire `get_camera_frame()` onto the broadcaster
    - In `src/backend/utils/camera_manager.py`, route `get_camera_frame(camera_id, config)` through `broadcaster.get_inference_frame(...)`, preserving the signature so `digital_input_*`, `workflow`, and capture endpoints are unchanged
    - _Requirements: 6.1, 6.3_

  - [x] 7.3 Implement out-of-range capture configuration validation
    - Validate per-capture image-source config against feature bounds in `backends.py`; reject out-of-range values with an error identifying the offending parameter and preserve the active config
    - _Requirements: 6.5_

  - [x]* 7.4 Write property test for inference claim reuse/fallback
    - **Property 16: Inference reuses the session claim, falls back when none** — claim count stays 1 with an active session; with none, opens exactly one dedicated claim and releases it (count returns to 0)
    - **Validates: Requirements 6.1, 6.3, 6.4**
    - `test/backend-test/utils/streaming/test_inference_reuse.py`

  - [x]* 7.5 Write property test for inference-failure session integrity
    - **Property 17: Inference failure leaves the session intact** — failed/unavailable inference request returns an error while the session stays RUNNING with its claim retained
    - **Validates: Requirements 6.6**
    - `test/backend-test/utils/streaming/test_inference_failure.py`

  - [x]* 7.6 Write property test for out-of-range capture rejection
    - **Property 21: Out-of-range capture configuration is rejected** — request rejected identifying an offending parameter, previously active config preserved
    - **Validates: Requirements 6.5**
    - `test/backend-test/utils/streaming/test_capture_validation.py`

  - [x]* 7.7 Write regression tests for `get_camera_frame` compatibility
    - Confirm `digital_input_*`, `workflow`, and capture callers keep working; extend `test/backend-test/utils/test_camera_manager.py`
    - _Requirements: 6.1, 6.3_

- [x] 8. Edit-settings interaction
  - [x] 8.1 Implement live `apply_settings` on a running session
    - Add `apply_settings(camera_id, features)` to `broadcaster.py`: apply gain/exposure/advanced features to the live session via the backend, keep session RUNNING and the viewer set unchanged, and keep the latest frame readable throughout
    - _Requirements: 5.1, 5.2, 5.3_

  - [x] 8.2 Implement settings-apply failure rollback
    - On any control failure, return an error naming the failed control, retain prior in-effect values, and keep the session active
    - _Requirements: 5.5_

  - [x] 8.3 Implement isolated single-frame override preview path
    - Add a single-frame override path (used by `POST /image-sources/{id}/preview`) that returns a preview reflecting the override without mutating the live session's applied values or the shared latest frame
    - _Requirements: 5.4_

  - [x]* 8.4 Write property test for live settings apply preserving the session
    - **Property 18: Live settings apply preserves the session** — backend reflects valid values, session stays RUNNING, viewer set unchanged, latest frame readable throughout
    - **Validates: Requirements 5.1, 5.2, 5.3**
    - `test/backend-test/utils/streaming/test_settings_apply.py`

  - [x]* 8.5 Write property test for settings-apply failure retention
    - **Property 19: Settings-apply failure retains prior values** — error identifies the failed control, prior values retained, session active
    - **Validates: Requirements 5.5**
    - `test/backend-test/utils/streaming/test_settings_failure.py`

  - [x]* 8.6 Write property test for override preview isolation
    - **Property 20: Override preview isolation** — override preview leaves the session's applied values and the shared latest frame unchanged
    - **Validates: Requirements 5.4**
    - `test/backend-test/utils/streaming/test_override_isolation.py`

- [x] 9. Stream API endpoints
  - [x] 9.1 Implement the `/streams/...` endpoints and register the router
    - Create `src/backend/endpoints/streams.py` with `POST /streams/{camera_id}/subscribe`, `GET /streams/{camera_id}/frame`, `POST /streams/{camera_id}/heartbeat`, `POST /streams/{camera_id}/unsubscribe`, `GET /streams/{camera_id}/viewers`, and `POST /streams/{camera_id}/settings` as thin clients over the broadcaster; register the router in `app.py`
    - _Requirements: 1.2, 2.3, 3.1, 3.6, 4.1, 4.6, 5.1, 5.2, 8.1, 8.2, 8.3, 8.4, 8.8_

  - [x] 9.2 Route the override preview endpoint to the isolated path
    - Wire `POST /image-sources/{id}/preview` (in `src/backend/endpoints/image_source.py`) to the single-frame override path from task 8.3
    - _Requirements: 5.4_

  - [x]* 9.3 Write endpoint serialization/status-code tests
    - Assert HTTP status mapping for OK / NO_FRAME / STALE / DISCONNECTED and subscribe reasons `viewer_limit` / `camera_unavailable`; cover the 8th-accepted / 9th-rejected boundary
    - Place in `test/backend-test/api-endpoints/test_streams_api.py`
    - _Requirements: 1.3, 1.6, 1.7, 2.6, 4.7, 7.5_

- [x] 10. Checkpoint - API and sharing complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 11. Frontend polling preview lifecycle
  - [x] 11.1 Wire the preview to subscribe/heartbeat/unsubscribe
    - Update the camera preview component to call subscribe on mount (store viewerId), poll the frame endpoint (doubling as heartbeat), and unsubscribe on unmount including `navigator.sendBeacon` on tab close
    - _Requirements: 3.6, 8.1, 8.2, 8.8_

  - [x] 11.2 Render frame-state and subscribe-rejection feedback
    - Handle NO_FRAME / STALE / DISCONNECTED frame states and `viewer_limit` / `camera_unavailable` subscribe rejections with clear user feedback instead of a frozen image
    - _Requirements: 1.6, 1.7, 2.6, 4.7, 7.5_

- [x] 12. Integration and performance tests (timing / real-device)
  - [x]* 12.1 First-frame and latency-bound integration tests
    - First frame within 10 s of session start; latest-frame read < 100 ms / < 500 ms; stale response < 500 ms
    - _Requirements: 2.3, 3.10, 4.1, 4.2, 4.7_
    - `test/backend-test/utils/streaming/integration/test_latency_bounds.py`

  - [x]* 12.2 Refresh-rate and viewer-independence performance tests
    - Refresh ≥ 5 fps for a ≥ 5 fps source, tracks device rate below that; refresh rate independent of viewer count up to 8
    - _Requirements: 4.3, 4.4, 4.5_
    - `test/backend-test/utils/streaming/integration/test_refresh_rate.py`

  - [x]* 12.3 New-subscriber and applied-setting timing tests
    - New subscriber starts receiving within 2000 ms with no disruption to existing viewers; applied setting visible within 5 frames
    - _Requirements: 1.4, 5.2_
    - `test/backend-test/utils/streaming/integration/test_subscriber_and_settings_timing.py`

  - [x]* 12.4 Inference-during-session timing tests
    - Inference grab during active session keeps viewer inter-frame gaps ≤ 500 ms and ≥ 90% rate; dedicated-claim grab returns within 2000 ms
    - _Requirements: 6.2, 6.3_
    - `test/backend-test/utils/streaming/integration/test_inference_timing.py`

  - [x]* 12.5 End-to-end fake-device tests for both backends
    - Run against a Fake Aravis camera (`Aravis.enable_interface("Fake")`) and a simulated GStreamer source with 1–3 examples each
    - _Requirements: 2.8_
    - `test/backend-test/utils/streaming/integration/test_end_to_end_backends.py`

- [x] 13. Final checkpoint - Ensure all tests pass
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional test sub-tasks and can be skipped for a faster MVP; core
  implementation tasks are never optional.
- Each task references granular requirement clauses for traceability; each property test names the
  design property number and the requirements it validates.
- Property tests use a mock `CameraBackend` + injected clock, run ≥ 100 iterations, and are
  parametrized over mock Aravis and GStreamer backends for parity (Req 2.8). Stateful properties
  (1, 2, 6, 8) use `RuleBasedStateMachine`.
- Wall-clock and real-device acceptance criteria are covered by the integration/performance tests
  in section 12, not by the property tests.
- Checkpoints provide incremental validation at natural boundaries.

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "2.1"] },
    { "id": 1, "tasks": ["1.2", "2.2"] },
    { "id": 2, "tasks": ["1.3", "2.3", "2.4"] },
    { "id": 3, "tasks": ["2.5", "3.1"] },
    { "id": 4, "tasks": ["3.2", "3.3", "3.4", "3.5", "3.6", "4.1"] },
    { "id": 5, "tasks": ["4.2", "4.3", "5.1"] },
    { "id": 6, "tasks": ["5.2"] },
    { "id": 7, "tasks": ["5.3"] },
    { "id": 8, "tasks": ["5.4"] },
    { "id": 9, "tasks": ["7.1", "5.5", "5.6", "5.7", "5.8", "5.9", "5.10", "5.11"] },
    { "id": 10, "tasks": ["8.1", "7.2", "7.3"] },
    { "id": 11, "tasks": ["8.2"] },
    { "id": 12, "tasks": ["8.3"] },
    { "id": 13, "tasks": ["9.1", "7.4", "7.5", "7.6", "7.7", "8.4", "8.5", "8.6"] },
    { "id": 14, "tasks": ["9.2", "9.3"] },
    { "id": 15, "tasks": ["11.1"] },
    { "id": 16, "tasks": ["11.2"] },
    { "id": 17, "tasks": ["12.1", "12.2", "12.3", "12.4", "12.5"] }
  ]
}
```
