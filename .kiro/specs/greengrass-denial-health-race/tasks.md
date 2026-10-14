# Implementation Plan

## Overview

This plan fixes the Greengrass denial health race using the exploratory bugfix workflow: reproduce
the on-device interleaving deterministically on UNFIXED code first (exploration test MUST FAIL),
record the passing preservation baseline, apply the contained fix in `trigger_runtime.py` (denial
branch reorders `_mark_denied` + generation bump BEFORE `_close_quietly`; the `reconnect()`
`_denied` short-circuit re-asserts the failed denial health before returning `RECONNECT_PARK`; a
contract comment in `ReconnectEngine`'s `RECONNECT_PARK` branch), then verify the fix and re-run
preservation, finish with the full workflow_engine suite checkpoint. The exploration test uses a
stub IPC client whose denial ALSO fires `on_stream_closed` on the same-generation handler
(mirroring the SDK behavior observed on ryan-orin-nano), with the engine driven synchronously via
a deferred-spawn harness (`spawn` captures the loop; the test drains it after `_mark_denied`
completes) and a non-blocking waiter — zero threads, zero sleeps. Final on-hardware
re-verification rides the NEXT LocalServer JP6 build (same build as the stale-workflow-registrations
fix) and runs only with the user's explicit go-ahead.

Suite invocation: `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/ -q`
Ignorable pre-existing failures: test_stale_registrations_exploration.py (5 — the separate
in-flight stale-workflow-registrations bugfix).

## Task Dependency Graph

```json
{
  "waves": [
    {
      "wave": 1,
      "tasks": ["1", "2"],
      "description": "Against UNFIXED code: task 1 writes the exploration test (MUST FAIL); task 2 records the preservation baseline (MUST PASS). Independent of each other."
    },
    {
      "wave": 2,
      "tasks": ["3"],
      "description": "Apply the fix (3.1 denial-branch reorder + generation bump, 3.2 short-circuit health re-assert, 3.3 RECONNECT_PARK contract comment), then re-run the exploration test (3.4) and the preservation suites (3.5). Depends on wave 1."
    },
    {
      "wave": 3,
      "tasks": ["4"],
      "description": "Checkpoint — full workflow_engine suite green (modulo the 5 ignorable stale-registrations failures). Depends on wave 2."
    },
    {
      "wave": 4,
      "tasks": ["5"],
      "description": "On-hardware re-verification on ryan-orin-nano riding the NEXT LocalServer JP6 build (shared with the stale-workflow-registrations fix). Requires the user's explicit go-ahead. Depends on wave 3."
    }
  ]
}
```

- Tasks 1 and 2 are independent and MUST both complete before any fix code is written.
- Task 3's sub-tasks 3.4 and 3.5 depend on 3.1–3.3.
- Task 5 is user-gated: it consumes a ~1h gdk build and touches the live device; do not start it
  without explicit user coordination.

## Tasks

- [x] 1. Write the bug condition exploration test (BEFORE implementing the fix)
  - **Property 1: Bug Condition** — Denial end-state is deterministic under the raced interleaving; **Property 2: Bug Condition** — Interleaving universality; **Property 3: Bug Condition** — Short-circuit re-assert
  - **CRITICAL**: The raced-interleaving tests MUST FAIL on unfixed code — the failure confirms the defect exists
  - Create `test/backend-test/workflow_engine/test_denial_health_race_exploration.py`
  - Build the stub `awsiot` module fixture following test_mqtt_transports.py's sys.modules-injection discipline (no SDK required)
  - Build a stub IPC client whose `new_subscribe_to_iot_core` captures the handler, whose `get_response().result(...)` raises the stub `UnauthorizedError`, and whose `close()` fires `handler.on_stream_closed()` — the same-generation denial teardown callback the existing stubs omit (mirrors the SDK behavior observed on-device)
  - Drive a real `ReconnectEngine` deterministically: `waiter=lambda delay: False` (non-blocking), deferred-spawn harness (`spawn` appends the loop callable to a list; the test drains it AFTER `worker.start()` returns, i.e. after `_mark_denied` completed) — reproducing the exact on-device thread interleaving with zero threads and zero sleeps
  - Interleaving (iii) test (deferred spawn, the on-device shape): assert `health.state == HEALTH_FAILED` and `to_wire()["lastError"] == greengrass_subscribe_denial_message(topic)` after quiescence — FAILS on unfixed code (yields `reconnecting`, `reconnectAttempts >= 1`)
  - Interleaving (ii) test (inline `spawn=lambda fn: fn()`, callback fired synchronously inside `_close_quietly`): assert the same end-state; document that this variant alone can mask the defect (inline execution lets `_mark_denied` land last) — the deferred variant is the reproducing one
  - Short-circuit re-assert test (Property 3): denied worker, health corrupted to `reconnecting` (as the raced loop does), `reconnect()` returns `RECONNECT_PARK`, health re-asserted `failed` + denial message, no new IPC connect — FAILS on unfixed code (no re-assert exists)
  - Run: `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/test_denial_health_race_exploration.py -q` and confirm the expected FAILURES
  - _Requirements: 1.1, 1.2, 1.3, 2.1, 2.2, 2.3_

- [x] 2. Record the preservation baseline (BEFORE implementing the fix)
  - **Property 4: Preservation** — Existing denial tests unchanged; **Property 5: Preservation** — Non-denial reconnect and transport behavior byte-identical
  - **CRITICAL**: These suites MUST PASS on unfixed code — they define the behavior the fix must not change
  - Run: `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/test_mqtt_transports.py test/backend-test/workflow_engine/test_property_reconnect.py test/backend-test/workflow_engine/test_property_qos_clamp.py test/backend-test/workflow_engine/test_trigger_lifecycle.py -q`
  - Confirm all pass, with particular attention to the three denial tests in test_mqtt_transports.py and Property 12 semantics in test_property_reconnect.py
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

- [x] 3. Apply the fix in `src/backend/workflow_engine/trigger_runtime.py`
- [x] 3.1 Reorder the denial branch of `GreengrassIpcSubscriber._subscribe` (fix a)
  - In the `except model.UnauthorizedError:` handler: bump `self._generation` under the lock and call `self._mark_denied()` BEFORE `self._close_quietly(operation, ipc_client)`, with a comment naming the race (denial teardown fires same-generation stream callbacks; `_handle_stream_lost` must suppress them via `_denied` and the stale generation)
  - Module stays import-light: no new imports; uses only existing module-level symbols
  - _Requirements: 2.1, 2.2_

- [x] 3.2 Re-assert the denial health in the `reconnect()` `_denied` short-circuit (fix b)
  - Read `self._denied` under the lock; when denied, set `HEALTH_FAILED` with `greengrass_subscribe_denial_message(self.topic)` on the health handle (outside the worker lock — `TriggerHealth` has its own lock) before returning `RECONNECT_PARK`, with a comment explaining the raced-in loop's pre-attempt `set_state(reconnecting)` overwrite this neutralizes
  - _Requirements: 2.3_

- [x] 3.3 Extend the `RECONNECT_PARK` contract comment in `ReconnectEngine._reconnect_loop` (fix c)
  - Documentation only, no executable change: the worker owns the FINAL health write on park — the engine never writes health after receiving `RECONNECT_PARK`; workers must (re-)assert their permanent-failure health inside the attempt that returns it
  - _Requirements: 2.2_

- [x] 3.4 Verify the fix: re-run the exploration test (MUST now PASS)
  - Run: `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/test_denial_health_race_exploration.py -q`
  - All interleavings end `failed` + denial message; the denial contributes no genuine-loss attempt against `retry_limit`
  - _Requirements: 2.1, 2.2, 2.3_

- [x] 3.5 Verify preservation: re-run the task 2 baseline suites (MUST still PASS unchanged)
  - Run: `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/test_mqtt_transports.py test/backend-test/workflow_engine/test_property_reconnect.py test/backend-test/workflow_engine/test_property_qos_clamp.py test/backend-test/workflow_engine/test_trigger_lifecycle.py -q`
  - No test modified, no test skipped; Property 12 semantics untouched; non-denial reconnect behavior byte-identical
  - _Requirements: 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

- [x] 4. Checkpoint — full workflow_engine suite
  - Run: `PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/ -q`
  - Green except the 5 ignorable pre-existing failures in test_stale_registrations_exploration.py (the separate in-flight stale-workflow-registrations bugfix); no NEW failures anywhere
  - _Requirements: 2.1, 2.2, 2.3, 3.1, 3.2, 3.3, 3.4, 3.5, 3.6, 3.7_

- [x] 5. On-hardware re-verification (USER-GATED — rides the next LocalServer JP6 build)
  - **Do not start without the user's explicit go-ahead**: consumes a ~1h gdk build and touches the live device; shares the build with the stale-workflow-registrations fix
  - Build and publish the next `aws.edgeml.dda.LocalServer.arm64JP6` version; deploy to ryan-orin-nano
  - Register a trigger-driven workflow whose MQTT topic is NOT covered by the component's `aws.greengrass.ipc.mqttproxy` accessControl policy
  - Confirm on FIRST registration the Trigger_Health API surfaces state `failed` with the accessControl denial message (`greengrass_subscribe_denial_message` text naming the topic) — not `reconnecting` — and `reconnectAttempts` stays 0 for the denial
  - Confirm the engine still logs "parked: permanent failure"
  - _Requirements: 2.1, 2.2, 2.3_

## Notes

- Tasks 1 and 2 run against UNFIXED code: task 1's raced-interleaving tests MUST FAIL (confirming
  the defect) and task 2's suites MUST PASS (defining the preservation baseline) before any line of
  task 3 is written.
- The deferred-spawn harness is the load-bearing reproduction detail: a purely inline
  `spawn=lambda fn: fn()` lets `_mark_denied` land as the final health write and masks the defect.
  Draining the captured loop AFTER `worker.start()` returns pins the on-device thread interleaving
  deterministically.
- The fix is contained to `src/backend/workflow_engine/trigger_runtime.py` and adds no imports —
  the module's import-light discipline (importable without the SDK or `COMPONENT_WORK_PATH`) is
  unchanged.
- `ReconnectEngine` has no executable change (fix c is a comment), so trigger-activation-runtime
  Property 12 semantics are structurally untouched.
- The 5 failures in test_stale_registrations_exploration.py are pre-existing and belong to the
  separate in-flight stale-workflow-registrations bugfix; they are the only ignorable failures in
  the task 4 checkpoint.
- Task 5 is user-gated and rides the same LocalServer JP6 build as the stale-workflow-registrations
  fix — coordinate with the user before building or deploying.
