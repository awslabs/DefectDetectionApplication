# Bugfix Requirements Document

## Introduction

During on-hardware verification of the trigger-activation-runtime feature (device ryan-orin-nano, `aws.edgeml.dda.LocalServer.arm64JP6` v1.0.51, 2026-08-05), a minor race condition was observed in the Greengrass IPC subscribe transport (`GreengrassIpcSubscriber` in `src/backend/workflow_engine/trigger_runtime.py`). When the Greengrass nucleus DENIES a `SubscribeToIoTCore` request (`UnauthorizedError`), Requirement 8.7 of trigger-activation-runtime demands that the trigger node's Trigger_Health deterministically read `failed` with the actionable accessControl denial message, with no reconnect attempted and nothing counted against `retry_limit`. On the very first registration on-device, the API instead briefly-then-persistently showed `reconnecting` with `reconnectAttempts` 1.

The root cause is an ordering race inside the denial branch: when the subscribe activation is denied, the SDK can also fire `on_stream_closed`/`on_stream_error` on the stream handler while the worker closes the denied operation. That handler was created under the CURRENT subscribe generation (the generation guard only suppresses STALE generations), and at that instant `_denied` is not yet set — `_mark_denied` runs after `_close_quietly` in `_subscribe`'s denial branch. The same-generation stream-lost callback therefore reaches `on_connection_lost`, the ReconnectEngine writes health `reconnecting` and starts a loop, and depending on thread scheduling the loop's pre-attempt `reconnecting` write can land AFTER `_mark_denied`'s `failed` write; the attempt then returns `RECONNECT_PARK` and the engine parks WITHOUT restoring health, leaving the wrong state surfaced permanently. Device evidence: the first registration showed `reconnecting`/attempt-1 while the log showed the park ("reconnect to ... parked: permanent failure"); a clean re-registration surfaced the correct `failed` state with the denial message.

The defect is a health-reporting race only — the denial is still parked and never retried indefinitely — but it hides the actionable accessControl diagnostic behind a misleading `reconnecting` state on exactly the occasion the operator needs it.

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN a `SubscribeToIoTCore` denial's operation close causes the SDK to fire `on_stream_closed`/`on_stream_error` on the same-generation stream handler before `_mark_denied` has set `_denied` THEN the system routes the stream-lost signal into `on_connection_lost` and starts a reconnect loop for a permanent authorization denial

1.2 WHEN the reconnect loop started by such a same-generation denial callback is scheduled so that its pre-attempt `reconnecting` health write lands after `_mark_denied`'s `failed` write THEN the system leaves Trigger_Health at `reconnecting` (with the stream-closed error as `lastError`) after the attempt returns `RECONNECT_PARK` and the engine parks without restoring health

1.3 WHEN a reconnect loop slips in on a denial THEN the system records a reconnect attempt (`reconnectAttempts` 1) and performs an extra denied subscribe attempt for a failure mode that must never be retried or counted

### Expected Behavior (Correct)

2.1 WHEN a `SubscribeToIoTCore` denial's operation close fires `on_stream_closed`/`on_stream_error` on the same-generation stream handler at any point during the denial handling THEN the system SHALL suppress the stream-lost signal so no reconnect loop is started for the denial

2.2 WHEN all processing of a `SubscribeToIoTCore` denial settles, regardless of callback interleaving THEN the system SHALL leave Trigger_Health at `failed` with `greengrass_subscribe_denial_message(topic)` as the last error

2.3 WHEN a reconnect loop nevertheless slips in around a denial (any interleaving) THEN the system SHALL re-assert the `failed` denial health before parking, so the loop's pre-attempt `reconnecting` write can never be the settled state, and the denial SHALL NOT count against `retry_limit`

### Unchanged Behavior (Regression Prevention)

3.1 WHEN an `UnauthorizedError` denial occurs at subscribe time without any stream callback firing THEN the system SHALL CONTINUE TO mark health `failed` with the accessControl denial message naming the topic and `aws.greengrass.ipc.mqttproxy`, without raising from `start()` and without calling `on_connection_lost`

3.2 WHEN `reconnect()` is called on a worker already marked denied THEN the system SHALL CONTINUE TO return `RECONNECT_PARK` without another subscribe attempt

3.3 WHEN a denial is hit inside an engine-driven reconnect (a previously healthy subscription drops and the reconnect attempt is denied) THEN the system SHALL CONTINUE TO park the engine on that attempt with the denial health standing and no further attempts, even with `retry_limit` 0

3.4 WHEN a non-denial stream loss occurs on a healthy subscription THEN the system SHALL CONTINUE TO route it to `on_connection_lost` so the ReconnectEngine drives `reconnecting` health, backoff attempts, restoration on success, and `failed`-on-exhaustion exactly as today (Property 12 of trigger-activation-runtime untouched)

3.5 WHEN a stream callback arrives from a stale (torn-down) subscribe generation, or while the worker is stopping THEN the system SHALL CONTINUE TO ignore it

3.6 WHEN a subscription succeeds THEN the system SHALL CONTINUE TO subscribe with the configured topic filter and the qos clamped to the Greengrass maximum, set health `subscribed`, and deliver stream events as Trigger_Context via `on_delivery`

3.7 WHEN a non-denial error occurs at subscribe time THEN the system SHALL CONTINUE TO close the operation and client quietly and propagate the error (a failed reconnect attempt for the engine; start containment for the group)
