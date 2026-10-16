# Design Document: localserver-config-live-update

## Overview

`triton-model-cleanup-wedge` made LocalServer's dependency on
`aws.greengrass.Cli` SOFT. Deployments stopped restarting LocalServer as a
side effect of the CLI restart, so a deployment that only merges LocalServer
configuration no longer reaches the keys that the backend reads once at
startup (requirements.md, Requirement 1). Today an operator applies them by
restarting LocalServer by hand (that spec's Operator notes).

This design adds a `Config_Update_Watcher` to the backend:

1. It subscribes to its own component configuration with the Greengrass IPC
   operation `SubscribeToConfigurationUpdate`.
2. On a notification it waits for the burst to settle, re-reads the whole
   configuration once with the existing `DefectDetectionConfig.get_component_config`,
   and compares each startup-only key with the value it applied last.
3. For each changed key it calls the Applier that the key's owning module
   registered. A key without an Applier, or one whose Applier cannot apply the
   change in place, leads to one explicit, logged and rate-limited backend
   restart that waits until no run is in flight.
4. If the subscription cannot be opened or closes, it polls instead.

Keys that are already live (`LocalLoginEnabled`, `ModelLoadSelfHealEnabled`,
`accessControl`) are not touched. A notification that changes no startup-only
key does nothing.

This spec is documents only. Nothing here is implemented by the
`triton-model-cleanup-wedge` change.

## Design Decisions

### 1. Subscribe to the component's own configuration over IPC

The backend already holds one process-wide Greengrass IPC client
(`utils/ipc_client.get_ipc_client`) and reads its own configuration with
`GetConfiguration` (`defect_detection_config.py:61-91`). The component
configuration IPC service also offers `SubscribeToConfigurationUpdate`: a
component subscribes to a key path (an empty list covers its whole
configuration), and each event carries only the component name and the key
path that changed, not the value
([Interact with component configuration](https://docs.aws.amazon.com/greengrass/v2/developerguide/ipc-component-configuration.html)).
So the watcher subscribes once with `key_path=[]` and no `component_name`,
which defaults to the calling component, and treats every event only as a
signal to re-read. Re-reading through `get_component_config` keeps one parser
and one error path for both the startup read and the update.

**Authorization.** The AWS documentation for the component configuration
service lists `GetConfiguration`, `UpdateConfiguration`,
`SubscribeToConfigurationUpdate`, `SubscribeToValidateConfigurationUpdates`
and `SendConfigurationValidityReport` and gives no authorization policy for
them, while the pages of the services that need one (shadows, local
publish/subscribe, the Greengrass CLI, component lifecycle) each say that a
custom component must define authorization policies to use them
([Interact with component configuration](https://docs.aws.amazon.com/greengrass/v2/developerguide/ipc-component-configuration.html);
[Use the AWS IoT Device SDK to communicate with the Greengrass nucleus](https://docs.aws.amazon.com/greengrass/v2/developerguide/interprocess-communication.html),
which says that only some IPC operations need an `accessControl` policy;
[Manage local deployments and components](https://docs.aws.amazon.com/greengrass/v2/developerguide/ipc-local-deployments-components.html)
and [Interact with local shadows](https://docs.aws.amazon.com/greengrass/v2/developerguide/ipc-local-shadows.html)
for the contrast). The LocalServer recipes grant no configuration policy
today, and `GetConfiguration` on LocalServer's own configuration works on
every device, which agrees with the documentation. So no recipe change is
planned. The on-device verification confirms that the subscription opens
without an `UnauthorizedError`; if it does not, the fallback of decision 5
applies and the recipe gains the policy in the same change.

`SubscribeToValidateConfigurationUpdates` is not used: it exists to reject a
configuration before the nucleus applies it, which this feature does not need,
and the documentation notes that local deployments do not notify it.

### 2. Per-key Appliers registered by the owning modules

Each startup-only key belongs to one module that knows how to apply it, so the
watcher holds no key-specific logic. A module registers
`register_applier(keys, apply_fn, name)` at import or at its own start, where
`apply_fn(old_config, new_config) -> ApplyResult` returns `applied`,
`invalid(reason)` or `needs_restart(reason)`. The Appliers planned for the keys
of Requirement 1:

- **`workflowTuning`, the sample export reconfigure**
  (`workflow_engine/runtime.py`, `workflow_engine/tuning/sample_export.py`).
  The Applier parses the new section with `ExportConfig.from_component_configuration`.
  A malformed section is `invalid`. When export stays enabled, it builds a new
  `SampleExporter` with `configure_sample_exporter` (which installs it with
  `set_sample_exporter`, `sample_export.py:667`) and drains the old one the
  way `shutdown_sample_exporter` (`:721`) does, so in-flight exports finish. When export is turned on or off,
  the Device_Score_Job runner and the one-shot backfill, which
  `start_workflow_engine` starts only with an exporter (`runtime.py:111-133`),
  would have to start or stop as well; that transition returns
  `needs_restart("tuning export turned on or off")` in the first version.
- **`StationName`, `SoftwareVersion`, `WebuxUrl`, `TenantID`, `DeviceID`, the
  `DefectDetectionConfig` cache invalidation**
  (`defect_detection_config/defect_detection_config.py`). These are read on
  every request through the per-instance cache of `get_local_server_config()`
  (`:132-142`). The Applier clears `config_cache` on every instance (the
  module keeps a weak set of instances, because `endpoints/system.py:295`,
  `utils/server_setup.py:75` and other modules each create their own), so the
  next request reads the new values. It always returns `applied`.
- **`CameraDiscoveryIntervalSeconds`, the camera-discovery reschedule**
  (`camera_discovery/discovery.py`). The interval is resolved once in
  `CameraDiscovery.start` (`:273`). The Applier gains a
  `CameraDiscovery.set_interval(seconds)` that stores the value resolved by
  the existing `_resolve_interval` rules (`:345-372`: non-numeric or
  non-positive is `invalid`, with today's WARNING). `_run_loop` (`:340-343`)
  waits on the stop event for the interval; it gains a separate wake event,
  which `set_interval` sets, so the current wait ends and the next cycle uses
  the new interval at once, without a second loop and without stopping the
  loop.

A key that a later change adds and that has no Applier is handled by
decision 3, so a missing Applier never means a silently ignored change.

### 3. Explicit restart for keys without an Applier

A key without an Applier, or an Applier that answers `needs_restart`, needs a
process restart. The watcher performs it itself instead of waiting for an
operator, because the point of this feature is that a deployment takes
effect. It uses the restart path of `triton-model-cleanup-wedge`'s self-heal
(design.md D7): `run_activity` registration, `begin_quiesce()`, a drain until
no run is registered, `commit_restart`, and SIGTERM to the backend's own PID
with the backstop, so the container's restart policy brings it back. Unlike
self-heal, it never interrupts a run: any registered run makes it wait, with
an INFO line naming the runs, and it re-checks every 30 s. Before the signal it
writes the reason (the keys and why) to a small record on the host volume and
logs one WARNING line; the next process logs the INFO "restarted to apply
<keys>". The rate limit (one per 10 min, 6 per 24 h, both persisted in the
record) keeps a deployment that flips a value repeatedly from turning into a
restart loop. When it is reached, one ERROR line names the pending keys and the
manual restart, and the keys are applied at the next restart of any kind.

### 4. Debounce

A deployment that merges several keys produces several events, one per key
path, within milliseconds, and a deployment can also update the configuration
in more than one step. The watcher therefore waits 2 s after the last event
(`CONFIG_UPDATE_DEBOUNCE_S = 2`, at most 10 s after the first event) before it
re-reads, so one deployment leads to one re-read, one set of Applier calls and
at most one restart (Requirement 2.6).

### 5. Fallback to periodic polling

The subscription can fail to open (an older nucleus, an IPC error, a missing
authorization) or close later (a nucleus restart closes every stream). In
either case the watcher logs one WARNING and polls: every 60 s it re-reads the
configuration and runs the same comparison, and every 5 min it tries to
subscribe again. Polling costs one `GetConfiguration` call per minute, the same
order as the existing `LocalLoginEnabled` poller (30 s,
`local_auth/config.py:51`). It keeps Requirement 2.1's 60 s bound.

## Components and Interfaces

- **`src/backend/utils/config_update_watcher.py`** (new).
  `ConfigUpdateWatcher(reader, ipc_client, clock, restarter, records_dir)`,
  `register_applier(keys, apply_fn, name)`, `start()`, `stop()`,
  `applied_view()`. Its baseline is the configuration read at start, so a key
  that the process already read at start is never applied twice.
- **The comparison.** For each registered key, and for every key of the
  inventory without an Applier, the canonical JSON of the old and new value is
  compared. Live keys (`LocalLoginEnabled`, `ModelLoadSelfHealEnabled`,
  `accessControl`) are on an explicit ignore list, and unread keys are
  ignored, so neither causes an Applier call or a restart (Requirement 4.1).
- **Applied-configuration view.** Per startup-only key: set or not, a
  fingerprint (the first 12 hex characters of the sha256 of its canonical
  JSON; the value itself is never shown, because `workflowTuning` names a
  bucket and may later hold other settings), applied when, and how. It is
  served by `GET /system/configuration-status` behind the existing
  authorization (Requirement 5.1).
- **Restart record.** `/aws_dda/localserver/config_restart.json` (a new
  directory on the `/aws_dda` host volume, so the record survives the restart
  it causes), written atomically: the attempts of the last 24 h and the
  pending keys.

## Error Handling

| Operation | Failure | Result | Log |
|---|---|---|---|
| Subscribe | raises, or `UnauthorizedError` | polling fallback; a new subscription every 5 min | WARNING once per streak |
| Subscription stream | closed or errored | polling fallback | WARNING |
| Re-read | IPC error, timeout, non-dict, `{}`, no `StreamShadowName` | the applied configuration stays; retry no sooner than 60 s | WARNING once per streak (and `get_component_config`'s own ERROR) |
| Applier | `invalid(reason)` | the key keeps its previous value; no restart for it | WARNING naming the key and the reason |
| Applier | raises | the key keeps its previous value; the other keys go on | the exception, once |
| Explicit restart | a run is registered | wait, re-check every 30 s; no run is held or interrupted | INFO naming the runs, once per wait |
| Explicit restart | rate limit reached | no restart; the keys stay pending | ERROR naming the keys and the manual restart |
| Explicit restart | the record cannot be written | no restart | ERROR |

## Testing Strategy

- **Unit tests** (`test/backend-test/config_live_update/`, host-side, with a
  fake IPC client that delivers events and answers `GetConfiguration`):
  - a burst of events within 2 s leads to one re-read;
  - a changed `workflowTuning` calls only its Applier, and an unchanged
    configuration calls nothing;
  - a change of `LocalLoginEnabled` or `ModelLoadSelfHealEnabled` alone calls
    nothing;
  - a key without an Applier leads to one restart request with the WARNING
    naming it; two such keys in one deployment lead to one request;
  - an `invalid` answer keeps the previous value and requests no restart;
  - a failed re-read keeps the applied configuration and waits 60 s;
  - a subscription that raises or closes switches to polling, which detects
    a change within 60 s, and re-subscribes after 5 min;
  - the rate limit across a simulated restart (a new watcher reading the same
    record) and the ERROR when it is reached;
  - with a run registered in `run_activity`, the restart waits and the run
    finishes normally.
- **Applier tests**, one per owning module: the sample exporter is replaced and
  the old one drained; enabling or disabling export answers `needs_restart`;
  the `DefectDetectionConfig` cache is cleared on every instance and the next
  `get_station()` returns the new `StationName`; `set_interval` applies a
  valid value at once and rejects a non-numeric or non-positive one with
  today's WARNING.
- **Preservation:** the existing suites of `local_auth`, `camera_discovery`,
  `workflow_engine/tuning` and `defect_detection_config` pass unchanged.
- **On-device (JP5, the MIC-730):** a cloud deployment that merges only a new
  `workflowTuning` is applied in place within 60 s; a local
  `greengrass-cli deployment create --update-config` merge is applied too
  (whether local deployments notify `SubscribeToConfigurationUpdate` is
  checked there); a deployment that merges only a key without an Applier
  leads to one logged restart; a deployment that changes nothing does
  nothing; a manual workflow run during the restart wait finishes. Other
  arches only with the owner's go per device.

## Retiring the Interim Procedure

`triton-model-cleanup-wedge`'s bugfix.md has an Operator-notes section,
"Interim behavior change from the SOFT CLI dependency (2.9)", that tells
operators to restart LocalServer by hand after a deployment that changes a
startup-only key. When this feature is verified on the device:

1. That section is replaced by a short note: startup-only keys take effect
   within 60 s of a deployment, in place or through one logged backend
   restart, and the manual restart is needed only after the rate limit's
   ERROR line.
2. bugfix.md 2.9's sentence about the interim behavior gains a pointer to
   this spec, and its revision history gains a dated entry.
3. The three manual restart methods stay documented as general operations,
   not as a required step after a deployment.

## Out of Scope

- The configuration of other components that the backend reads (ShadowManager's
  size limit is already live; the model components' configuration cache in
  `utils/feature_configs_utils.py:79-80`).
- Rejecting invalid configuration at deployment time
  (`SubscribeToValidateConfigurationUpdates`).
- Starting or stopping the tuning job runner and backfill in place.
- A Portal view of the applied configuration.
- Any change to the CLI dependency type, which stays SOFT.
