# Greengrass Denial Health Race Bugfix Design

## Overview

On-hardware verification of trigger-activation-runtime (ryan-orin-nano, `aws.edgeml.dda.LocalServer.arm64JP6` v1.0.51, 2026-08-05) surfaced a health-reporting race in `GreengrassIpcSubscriber` (`src/backend/workflow_engine/trigger_runtime.py`). When the nucleus DENIES `SubscribeToIoTCore` (`UnauthorizedError`), Trigger_Health must deterministically end at `failed` with `greengrass_subscribe_denial_message(topic)` (trigger-activation-runtime Requirement 8.7). On the first on-device registration it instead settled at `reconnecting` with `reconnectAttempts` 1, hiding the actionable accessControl diagnostic; the log simultaneously showed the ReconnectEngine park ("reconnect to ... parked: permanent failure"). A clean re-registration surfaced the correct `failed` + denial message.

Code inspection confirms the hypothesized mechanism (verified against the current `trigger_runtime.py`):

- In `_subscribe`'s denial branch, `self._close_quietly(operation, ipc_client)` runs BEFORE `self._mark_denied()`. Closing the denied operation can make the SDK fire `on_stream_closed`/`on_stream_error` on the stream handler.
- That handler was created under the CURRENT subscribe generation; `_handle_stream_lost` suppresses only `_stopping`, `_denied`, or a stale generation. In the denial window `_denied` is still False and the generation matches (the denial branch calls `_close_quietly` directly, not `_teardown_transport`, so no generation bump) — the signal passes straight to `on_connection_lost`.
- The ReconnectEngine spawns its loop on a separate thread. The loop writes `HEALTH_RECONNECTING` at the top of each iteration and, on `RECONNECT_PARK`, parks WITHOUT touching health (by design — the worker owns the denial health). `reconnect()`'s `_denied` short-circuit returns `RECONNECT_PARK` without re-asserting the denial health.
- Thread scheduling therefore decides the settled state: when the loop's pre-attempt `reconnecting` write lands after `_mark_denied`'s `failed` write, the park leaves `reconnecting`/attempt-1 standing forever. That is the device-observed state.

The fix is the minimal correct combination of the two directions evaluated:

1. **(a) Mark the denial before closing the denied operation**: in `_subscribe`'s `UnauthorizedError` branch, run `_mark_denied()` BEFORE `_close_quietly(operation, ipc_client)`, so any same-generation stream callback fired by the close is suppressed by the existing `_denied` guard in `_handle_stream_lost`. No generation bump is needed — the denial is permanent for the worker's lifetime, and `_denied` suppression is total.
2. **(b) Re-assert the denial health in `reconnect()`'s `_denied` short-circuit**: before returning `RECONNECT_PARK`, write `HEALTH_FAILED` with `greengrass_subscribe_denial_message(self.topic)`. This closes every remaining interleaving: a loss signal that slips through concurrently (e.g. an SDK-thread `on_stream_error` racing the denial branch between the raise and `_mark_denied`'s lock acquisition) starts a loop whose pre-attempt `reconnecting` write is then overwritten by the short-circuit's `failed` re-assert before the engine parks — the settled state is always the denial health.

Both changes are confined to `GreengrassIpcSubscriber`; the `ReconnectEngine`, `TriggerHealth`, and every other transport are untouched, and the module stays import-light (no new imports).

## Glossary

- **Bug_Condition (C)**: A `SubscribeToIoTCore` denial whose same-generation stream-lost callback fires before `_mark_denied` completes, resulting in a settled Trigger_Health state != `failed` (or lastError != the denial message) after all processing settles.
- **Property (P)**: On a denial, health deterministically ends at `failed` with `greengrass_subscribe_denial_message(topic)` regardless of callback interleaving; the denial never counts against `retry_limit`; no reconnect loop runs — or one that slips in must not leave a non-failed state.
- **F / F'**: `GreengrassIpcSubscriber` before / after the fix.
- **denial window**: The interval inside `_subscribe`'s `except model.UnauthorizedError:` branch between the raise and `_mark_denied` setting `_denied` — today spanning `_close_quietly(operation, ipc_client)`, which is what makes the close-triggered callback race possible.
- **same-generation callback**: An `on_stream_closed`/`on_stream_error` delivered to the handler created by the CURRENT `_subscribe` call. `_handle_stream_lost`'s generation guard only blocks STALE generations (handlers of torn-down operations); a same-generation callback is suppressed only by `_stopping` or `_denied`.
- **pre-attempt write**: `_reconnect_loop`'s `set_state(HEALTH_RECONNECTING, error=...)` at the top of each iteration, executed before the backoff wait and the attempt.
- **park (RECONNECT_PARK)**: The engine's permanent-failure exit: no further attempts, no health write — the worker is trusted to have set its own `failed` record (trigger-activation-runtime Requirement 8.7). Correct for the denial-during-reconnect path; the gap was the loop that should never have started.
- **`_mark_denied`**: Sets `_denied` under the worker lock, then writes `HEALTH_FAILED` with the denial message and logs it.
- **denial message**: `greengrass_subscribe_denial_message(topic)` — the actionable text naming the topic and the `aws.greengrass.ipc.mqttproxy` accessControl configuration.
- **synchronous engine**: A `ReconnectEngine` built with `spawn=lambda fn: fn()` and a non-blocking waiter (`lambda delay: False`) — the loop runs inline and deterministically, no threads, no sleeps (the established pattern in `test_mqtt_transports.py` / `test_property_reconnect.py`).
- **deferred spawn**: `spawn=lambda fn: pending.append(fn)` with the captured loop run explicitly after the denial handling returns — the deterministic model of the device's thread scheduling where the loop's pre-attempt write lands after `_mark_denied`'s `failed` write.

## Bug Details

### Bug Condition

**Formal Specification:**

```
FUNCTION isBugCondition(X)
  INPUT: X of type DenialScenario
         {denialAtSubscribe, streamCallback, callbackGeneration, callbackTiming}
  OUTPUT: boolean

  RETURN X.denialAtSubscribe                          -- UnauthorizedError from activate/get_response
         AND X.streamCallback ∈ {on_stream_closed, on_stream_error, both}
         AND X.callbackGeneration = currentGeneration -- handler of THIS subscribe call
         AND X.callbackTiming = beforeMarkDeniedCompletes
         -- today: _close_quietly runs before _mark_denied, so the close-triggered
         -- callback always lands in this window; concurrent SDK-thread callbacks
         -- can land in it too
END FUNCTION
```

**Defect predicate on the unfixed code** (what C(X) produces):

```
settledHealth(F, X).state ≠ failed
  OR settledHealth(F, X).lastError ≠ greengrass_subscribe_denial_message(topic)
  OR reconnectLoopRan(F, X)        -- attempts recorded / extra denied subscribe
```

### Examples

- **Device incident (verified)**: first registration on ryan-orin-nano → nucleus denies `SubscribeToIoTCore` → same-generation stream-closed fires during the denial close → `on_connection_lost` → engine loop thread scheduled after `_mark_denied`: loop writes `reconnecting`, attempt 1 hits the `_denied` short-circuit, `RECONNECT_PARK`, park log emitted, health left `reconnecting`/attempt-1. Expected: `failed` with the accessControl denial message.
- **Inline interleaving (reproduced deterministically with a synchronous engine)**: the close-triggered callback runs the whole loop inline before `_mark_denied`: health ends `failed` (the outer `_mark_denied` runs last) but a reconnect loop ran anyway — `reconnectAttempts` 1, a second denied IPC connect, engine parked. Expected: no reconnect activity at all for a denial.
- **Non-bug (plain denial)**: the denied operation's close fires no stream callback — health `failed` with the denial message, no reconnect, `start()` does not raise. Already correct; must stay identical (3.1).
- **Non-bug (denial during engine-driven reconnect)**: healthy subscription drops, the engine's attempt is denied; `_subscribe` returns False after `_mark_denied`, `reconnect()` returns `RECONNECT_PARK`, engine parks with the denial health standing after exactly one attempt. Already correct; must stay identical (3.3).
- **Non-bug (ordinary stream loss)**: a healthy subscription's stream errors — routed to `on_connection_lost`, engine drives `reconnecting`/backoff/restore/exhaustion exactly as trigger-activation-runtime Property 12 specifies (3.4).

## Expected Behavior

On a `SubscribeToIoTCore` denial, for every callback interleaving:

```
// Property: Fix Checking
FOR ALL X WHERE isBugCondition(X) DO
  result ← settle(GreengrassIpcSubscriber'(X))   -- F': fixed worker
  ASSERT result.health.state = failed
  ASSERT result.health.lastError = greengrass_subscribe_denial_message(topic)
  ASSERT result.health.reconnectAttempts contributes nothing to retry_limit exhaustion
  ASSERT no reconnect loop ran for a close-triggered same-generation callback
         (attempts = 0, exactly one IPC connect, on_connection_lost not invoked)
END FOR
```

```
// Property: Preservation Checking
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR
```

### Preservation Requirements

**Unchanged Behaviors:**

- Plain denial at start (no stream callback): `failed` health with the message naming the topic and `aws.greengrass.ipc.mqttproxy`, `start()` does not raise, `on_connection_lost` never called (3.1 — `test_greengrass_denial_at_start_fails_health_with_access_control_message`).
- `reconnect()` on a denied worker: `RECONNECT_PARK` with no new subscribe attempt (3.2 — `test_greengrass_denied_worker_parks_reconnect_with_no_retries`).
- Denial hit inside an engine-driven reconnect: park on the first attempt, denial health standing, no further attempts even with `retry_limit` 0 (3.3 — `test_greengrass_denial_during_reconnect_parks_the_engine`).
- Non-denial stream loss on a healthy subscription: full ReconnectEngine behavior — `reconnecting` health with attempt counts, 1 s-doubling-capped-60 s backoff, restoration on success, `failed` + park on exhaustion (3.4 — `test_property_reconnect.py`, trigger-activation-runtime Property 12, untouched).
- Stale-generation and stopping-worker callbacks ignored (3.5 — existing generation-guard behavior; the fix adds no generation change).
- Successful subscribe path: topic filter, qos clamp to the Greengrass maximum, `subscribed` health, stream events delivered as Trigger_Context (3.6 — `test_mqtt_transports.py` wiring tests, `test_property_qos_clamp.py`).
- Non-denial subscribe errors: quiet close then raise (3.7).
- Group/manager lifecycle behavior (`test_trigger_lifecycle.py`) — the fix changes nothing above the worker.

**Scope:** All inputs that do NOT involve a `SubscribeToIoTCore` denial with a same-generation callback in the denial window are completely unaffected. The `ReconnectEngine`, `TriggerHealth`, the paho MQTT and OPC UA transports, the activation core, and the manager are untouched.

## Hypothesized Root Cause

Confirmed by code inspection; the exploration test reproduces each link deterministically on unfixed code:

1. **The denial window exposes an unsuppressed callback path**: `_subscribe`'s denial branch closes the denied operation (`_close_quietly`) BEFORE `_mark_denied` sets `_denied`. The SDK may deliver `on_stream_closed`/`on_stream_error` on the handler during that close.
2. **The generation guard does not apply**: the handler belongs to the CURRENT generation (the denial branch never bumps the generation — only `_teardown_transport` does), so `_handle_stream_lost`'s only applicable suppressions are `_stopping` (false) and `_denied` (not yet set). The loss signal reaches `on_connection_lost`.
3. **The engine starts a loop for a permanent failure**: `on_connection_lost` spawns `_reconnect_loop` on its own thread. The loop's pre-attempt write sets `reconnecting` and `record_reconnect_attempt` bumps the wire-visible counter.
4. **Scheduling decides the settled state**: when the loop thread runs after `_mark_denied`, its `reconnecting` write overwrites the `failed` denial health; the attempt then hits `reconnect()`'s `_denied` short-circuit, returns `RECONNECT_PARK`, and the engine parks WITHOUT touching health (correct for the denial-during-reconnect path it was designed for, wrong here) — `reconnecting`/attempt-1 is surfaced permanently.
5. **Why re-registration looked clean**: a fresh group builds fresh worker/engine/health objects, and on that run the interleaving landed benignly (or no callback fired), so `_mark_denied`'s `failed` stood.

## Correctness Properties

Property 1: Bug Condition - A denial's same-generation stream callback triggers no reconnect activity

_For any_ denial scenario in which closing the denied operation/client fires `on_stream_closed`, `on_stream_error`, or both on the same-generation handler (isBugCondition, close-triggered timing), the fixed worker SHALL suppress the signal: `on_connection_lost` is never invoked for the denial, no reconnect loop runs, `reconnectAttempts` stays 0, exactly one IPC connect is made, and the settled health is `failed` with `greengrass_subscribe_denial_message(topic)`.

**Validates: Requirements 2.1, 2.2**

Property 2: Bug Condition - A slipped-in reconnect loop settles at the denial health

_For any_ interleaving in which a reconnect loop nevertheless starts around a denial (deferred/scheduled after `_mark_denied` — the device-observed ordering — modeled by driving the engine with a deferred or synchronous spawn against a denied worker), the fixed worker's `reconnect()` `_denied` short-circuit SHALL re-assert `HEALTH_FAILED` with the denial message before returning `RECONNECT_PARK`, so the settled health is `failed` with the denial message for every `retry_limit` value (including 0), the engine parks on the first attempt, and the denial never contributes to `retry_limit` exhaustion.

**Validates: Requirements 2.2, 2.3**

Property 3: Preservation - Denial paths without a racing callback are unchanged

_For any_ plain denial at start (no stream callback), any `reconnect()` call on an already-denied worker, and any denial hit inside an engine-driven reconnect (NOT isBugCondition), the fixed worker SHALL behave identically to the original: `failed` health with the accessControl message, `start()` not raising, `on_connection_lost` not called, `RECONNECT_PARK` with no extra subscribe, park on the first denied reconnect attempt with the denial health standing.

**Validates: Requirements 3.1, 3.2, 3.3**

Property 4: Preservation - Non-denial transport and reconnect behavior is unchanged

_For any_ non-denial input — successful subscribes (topic/qos clamp wiring, `subscribed` health, Trigger_Context delivery), ordinary stream losses driving the full ReconnectEngine backoff/restore/exhaustion behavior (trigger-activation-runtime Property 12), stale-generation or stopping-worker callbacks, non-denial subscribe errors, and group lifecycle — the fixed code SHALL behave identically to the original (F(X) = F'(X)); the `ReconnectEngine` itself is not modified.

**Validates: Requirements 3.4, 3.5, 3.6, 3.7**

## Fix Implementation

### Changes Required

**File**: `src/backend/workflow_engine/trigger_runtime.py` (the only file changed; module stays import-light — no new imports)

**Class**: `GreengrassIpcSubscriber`

**Specific Changes**:

1. **(a) `_subscribe` denial branch — mark before close** (Requirements 2.1, 2.2):

```python
        except model.UnauthorizedError:
            # Mark the denial BEFORE closing the denied operation: the
            # close can fire on_stream_closed/on_stream_error on the
            # CURRENT-generation handler, and _handle_stream_lost's
            # _denied guard must already be armed so the callback can
            # never route a permanent authorization denial into the
            # reconnect engine (greengrass-denial-health-race).
            self._mark_denied()
            self._close_quietly(operation, ipc_client)
            return False
```

   (Today the order is `_close_quietly` then `_mark_denied`.) `_mark_denied` already sets `_denied` under the worker lock before writing the `failed` health, so the guard is armed before the first byte of the close runs. Observable health transitions on the plain denial path are unchanged (`connecting` → `failed`). No generation bump is added: `_denied` suppression is permanent and total for this worker, which is exactly the denial semantics (authorization does not self-heal).

2. **(b) `reconnect()` `_denied` short-circuit — re-assert the denial health** (Requirements 2.2, 2.3):

```python
        with self._lock:
            denied = self._denied
        if denied:
            # A reconnect loop that slipped in before the denial was
            # marked writes ``reconnecting`` pre-attempt, and the engine
            # parks on RECONNECT_PARK without touching health — so the
            # failed denial record must be re-established here to be the
            # settled state under every interleaving
            # (greengrass-denial-health-race).
            self._health.set_state(
                HEALTH_FAILED,
                error=greengrass_subscribe_denial_message(self.topic),
            )
            return RECONNECT_PARK
```

   The health write happens outside the worker lock (matching `_mark_denied`'s discipline — `TriggerHealth` has its own lock). Behavior for the already-covered "denied worker parks" path is unchanged except for the (idempotent) re-assert of the same `failed` record; `test_greengrass_denied_worker_parks_reconnect_with_no_retries` asserts no new subscribe attempt and the park return, both preserved.

**Explicitly NOT changed**: `ReconnectEngine` (its park-without-touching-health contract is correct and relied on by the exhaustion path), `_handle_stream_lost`'s guard structure, `_teardown_transport`, the success path, the `except Exception` branch, all other transports, and `TriggerHealth`.

### Why this combination is minimal and sufficient

- (a) alone closes the close-triggered callback path (the deterministic, always-present instance of the race) but not a concurrent SDK-thread callback landing between the `UnauthorizedError` raise and `_mark_denied` acquiring the lock.
- (b) alone would leave the denial counting a wire-visible attempt and doing an extra denied IPC connect on every close-triggered denial (the loop would still run), and would leave a transient `reconnecting` window.
- Together: (a) prevents the loop from ever starting on the ordinary path; (b) guarantees the settled state is the denial health under any interleaving that still slips a loop in. The denial never counts against `retry_limit` in either case — the short-circuit parks on the first attempt regardless of the limit.

## Testing Strategy

### Test Environment

`PYTHONPATH=src/backend:test/backend-test python3 -m pytest test/backend-test/workflow_engine/ -q` — no SDK, no nucleus socket, no database: the `ipc_connect` seam plus the `awsiot` `sys.modules` stubs (the established pattern in `test_mqtt_transports.py`). The 5 pre-existing `test_stale_registrations_exploration.py` failures belong to another spec and are ignorable.

### Bug Condition Exploration Test (MUST FAIL on unfixed code)

**File**: `test/backend-test/workflow_engine/test_greengrass_denial_race_exploration.py`

Reproduces the race deterministically with stub IPC clients: a `RacingStubIpcOperation`/`RacingStubIpcClient` whose denial ALSO fires the stream-lost callback(s) on the same-generation handler from inside `close()` — i.e. during `_close_quietly`, before `_mark_denied` completes — driven with a synchronous ReconnectEngine (`spawn=lambda fn: fn()`, non-blocking waiter `lambda delay: False`).

- **Property 1 (Hypothesis)**: generate the callback plan — which callbacks fire on close (`on_stream_closed`, `on_stream_error`, or both) and from which closable (operation, client, or both) — plus a qos value; build worker + health + engine (engine late-bound to `worker.reconnect` as the manager does); call `worker.start()`; assert `health.reconnect_attempts == 0`, exactly one IPC connect, the engine never activated/parked, and settled health `failed` with `greengrass_subscribe_denial_message(TOPIC)`. **FAILS on unfixed code**: the callback reaches `on_connection_lost`, the inline loop records attempt 1, a second denied connect happens, and the engine parks.
- **Property 2 (deterministic deferred-spawn case + Hypothesis over retry_limit)**: model the device interleaving — deny with the racing stub but `spawn=lambda fn: pending.append(fn)`; after `start()` returns (health now `failed` via `_mark_denied`), run the captured loop; assert settled health `failed` with the denial message and `parked`. **FAILS on unfixed code**: the loop's pre-attempt write leaves `reconnecting`/attempt-1 standing after the park — the exact device-observed wrong state. Also drive `engine.on_connection_lost` directly against an already-denied worker for generated `retry_limit` values (0, 1, small ints) and assert the settled state is `failed` + first-attempt park (never exhaustion counting).

### Fix Checking

Re-run the SAME exploration tests after the fix — they encode the expected behavior and MUST PASS on fixed code. Do not write new tests for this step.

### Preservation Checking

Run the existing trigger-activation-runtime suites UNCHANGED — no edits to any existing test:

- `test_mqtt_transports.py` — especially the three denial tests (3.1, 3.2, 3.3)
- `test_property_reconnect.py` — Property 12 untouched (3.4)
- `test_property_qos_clamp.py` (3.6)
- `test_trigger_lifecycle.py` (3.5, and the group/manager surface)
- The full `test/backend-test/workflow_engine/` suite as the final gate (modulo the 5 known `test_stale_registrations_exploration.py` failures owned by another spec).

### Unit Tests

The exploration file doubles as the regression suite for this fix; no additional unit tests are needed beyond it and the preserved existing suites.
