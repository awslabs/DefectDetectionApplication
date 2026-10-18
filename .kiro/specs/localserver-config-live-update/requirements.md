# Requirements Document

## Introduction

Until `triton-model-cleanup-wedge`, every LocalServer recipe declared a HARD
dependency on `aws.greengrass.Cli`. Greengrass restarts the CLI on nearly every
deployment, and the HARD dependency restarted LocalServer with it. That restart
had a side effect nobody designed: every deployment, including one that only
merged LocalServer configuration, restarted the backend, so configuration that
the backend reads only once at startup was applied by accident.

`triton-model-cleanup-wedge` (bugfix.md 2.9, design.md D10) makes the CLI
dependency SOFT, so a CLI restart no longer restarts LocalServer. Its interim
behavior is documented in that spec's Operator notes ("Interim behavior change
from the SOFT CLI dependency"): a deployment that changes LocalServer's version
still restarts it and applies everything, but a deployment that only merges
LocalServer configuration no longer restarts it, and keys that are read only at
startup take effect at the next LocalServer restart, which the operator forces
by hand (the UI's restart, `greengrass-cli component restart`, or a restart of
the backend container).

This feature removes that manual step. A deployment that changes a startup-only
LocalServer key SHALL take effect on the device without relying on any
Greengrass CLI restart: either the backend applies the new value in place,
through a subscription to its own configuration, or it restarts itself
explicitly, with a log line that names the keys and the reason. Keys that are
already read live keep their behavior, and a deployment that changes nothing
causes nothing.

The first key to fix is `workflowTuning`, which
`workflow_engine/runtime.py::_configure_sample_export` (`runtime.py:279-301`,
called once at `:100`) reads once at engine start. Requirement 1 inventories
every other key.

This spec is documents only. It is not implemented in the
`triton-model-cleanup-wedge` change.

## Glossary

- **LocalServer**: the Greengrass component `aws.edgeml.dda.LocalServer.<variant>`
  that runs the backend container (`src/backend`). Seven recipe variants exist
  at the repository root (`recipe.yaml`, `recipe-arm64.yaml`,
  `recipe-arm64-jp5.yaml`, `recipe-arm64-jp6.yaml`, `recipe-arm64-jp7.yaml`,
  `recipe-amd64.yaml`, `recipe-amd64-nvidia.yaml`).
- **Component_Configuration**: LocalServer's Greengrass component
  configuration: the recipe's `DefaultConfiguration` merged with every
  deployment's configuration update (`merge`/`reset`).
- **Config_Reader**: `DefectDetectionConfig`
  (`src/backend/defect_detection_config/defect_detection_config.py`).
  `get_component_config(name)` (`:61-91`) makes one IPC `GetConfiguration`
  call and is uncached; `get_local_server_config()` (`:132-142`) caches the
  first non-empty result per instance for the life of the process.
- **Live_Key**: a configuration key whose reader observes a changed value
  within a bounded time without a process restart.
- **Startup_Only_Key**: a configuration key whose value the backend reads once
  (or reads through the per-instance cache of `get_local_server_config()`), so
  a changed value takes effect only after a backend restart.
- **Unread_Key**: a recipe default that the backend never reads.
- **Applier**: a function, registered by the module that owns a
  Startup_Only_Key, that applies a new value of that key in the running process.
- **Config_Update_Watcher**: the new backend component that observes changes
  to the Component_Configuration and runs the Appliers.
- **Explicit_Restart**: a restart of the backend process that the backend
  starts itself because a changed key has no Applier, preceded by a log line
  that names the keys and the reason.
- **Interim_Procedure**: the manual restart described in
  `triton-model-cleanup-wedge`'s Operator notes.

## Requirements

### Requirement 1: Configuration key inventory

**User Story:** As a maintainer, I want every LocalServer configuration key
classified by how the backend reads it, so that I know which keys a deployment
can change without a restart and which need this feature.

#### Acceptance Criteria

1. THE design SHALL list every LocalServer configuration key, with its reader
   (file and line) and its class (Live_Key, Startup_Only_Key or Unread_Key),
   and it SHALL include at least the following, as found in the source tree
   this spec was written against:

   | Key | Source | Reader | Class |
   |---|---|---|---|
   | `workflowTuning` | deployment merge | `workflow_engine/runtime.py::_configure_sample_export` (`:279-301`, called at `:100`), parsed by `workflow_engine/tuning/sample_export.py` (`CONFIG_KEY` at `:93`, `configure_sample_exporter` at `:674`) | Startup_Only_Key |
   | `StationName` | recipe default (absent from the two amd64 recipes) | `DefectDetectionConfig.get_station` / `get_station_old` (`defect_detection_config.py:153`, `:172`; key at `:41`) through the cache of `get_local_server_config()` (`:132-142`); served by `endpoints/system.py:306-317` from the instance at `:295` | Startup_Only_Key |
   | `SoftwareVersion` | deployment merge | `get_station` (`defect_detection_config.py:158`; key at `:42`), through the same cache | Startup_Only_Key |
   | `WebuxUrl`, `TenantID`, `DeviceID` | deployment merge | `get_station` (`defect_detection_config.py:162-164`; keys at `:44-47`), through the same cache | Startup_Only_Key |
   | `CameraDiscoveryIntervalSeconds` | deployment merge | `CameraDiscovery._resolve_interval` (`camera_discovery/discovery.py:345-372`; key at `:74`), called once from `CameraDiscovery.start` (`:273`); its provider is the cached `get_local_server_config` of the instance at `utils/server_setup.py:75` (`:149-151`), started at `:194` | Startup_Only_Key |
   | `LocalLoginEnabled` | recipe default | `local_auth/config.py::_read_local_login_enabled_via_ipc` (`:81-114`), uncached `get_component_config` (`:111`), re-polled every 30 s (`POLL_INTERVAL_SECONDS` at `:51`) | Live_Key |
   | `ModelLoadSelfHealEnabled` | recipe default, added by `triton-model-cleanup-wedge` | that spec's load watchdog, through uncached `get_component_config` (its design.md D7, "The reader") | Live_Key |
   | `accessControl` | recipe default | applied by the Greengrass nucleus to IPC authorization; not read by the backend | Live_Key (nucleus) |
   | `StreamShadowName` | recipe default | not read for behavior; `triton-model-cleanup-wedge` uses its presence to recognize a successful configuration read | Unread_Key |
   | `AppRunnerShadowName`, `ComponentName` | recipe defaults (`ComponentName` absent from the two amd64 recipes) | not read; the component name is discovered with `ListComponents` (`defect_detection_config.py:93-127`) | Unread_Key |

2. THE inventory SHALL be checked against the source with `grep -n` for
   `get_local_server_config`, `get_component_config(`, `CONFIG_KEY`,
   `INTERVAL_CONFIG_KEY`, `STATION_NAME_KEY`, `SOFTWARE_VERSION_KEY` and
   `new_get_configuration` under `src/backend` when implementation starts, and
   any key found since SHALL be added with its class.
3. THE inventory SHALL name, as outside this feature, the configuration of
   other components that the backend reads: ShadowManager's document size
   limit (`camera_sync/agent.py:212`, uncached, already live) and the model
   components' configuration (`utils/feature_configs_utils.py:79-80`, cached
   by `lru_cache` per model id).

### Requirement 2: Startup-only keys take effect without a CLI restart

**User Story:** As an operator, I want a deployment that changes a
startup-only LocalServer key to take effect on its own, so that I no longer
have to restart LocalServer by hand after the Greengrass CLI dependency became
SOFT.

#### Acceptance Criteria

1. WHEN a deployment changes the value of a Startup_Only_Key THEN THE backend
   SHALL apply the new value within 60 s of the change reaching the device,
   without a Greengrass restart of LocalServer and without relying on a
   restart of `aws.greengrass.Cli`.
2. WHERE the key's owning module registers an Applier, THE backend SHALL apply
   the new value in the running process, and SHALL log one INFO line that
   names the key and says that it was applied in place.
3. WHERE a changed key has no Applier, or its Applier reports that it cannot
   apply the change in place, THE backend SHALL perform an Explicit_Restart.
   Before it restarts, it SHALL log one WARNING line that names every changed
   key that needs the restart and the reason ("no in-place applier" or the
   Applier's reason).
4. THE Explicit_Restart SHALL be rate-limited: at most one per 10 min and
   6 per 24 h. WHEN the limit is reached THEN THE backend SHALL NOT restart,
   SHALL log one ERROR line that names the pending keys and says how to
   restart by hand, and SHALL apply them at the next restart.
5. THE Explicit_Restart SHALL NOT cut off a workflow run or capture task in
   progress: it SHALL wait until none is in flight, using the run registration
   of `triton-model-cleanup-wedge` (`utils/run_activity.py`), and SHALL log
   that it is waiting and for which runs. No run SHALL ever be held because of
   it.
6. WHEN several keys change in one deployment THEN THE backend SHALL apply
   them together, after one re-read of the Component_Configuration, and SHALL
   perform at most one Explicit_Restart for them.

### Requirement 3: Read failures and invalid values

**User Story:** As an operator, I want a failed configuration read or a bad
value to leave the device running on its previous configuration, so that a
mistake in a deployment never takes LocalServer down.

#### Acceptance Criteria

1. IF the re-read of the Component_Configuration fails (an IPC error, a
   timeout, a non-dict, `{}`, or a dict without `StreamShadowName`) THEN THE
   backend SHALL keep the configuration it applied last, SHALL log one
   WARNING per failure streak, and SHALL retry no sooner than 60 s later.
2. IF an Applier rejects a new value as invalid THEN THE backend SHALL keep
   that key's previous value, SHALL log one WARNING that names the key and
   the reason, and SHALL NOT perform an Explicit_Restart for that key.
3. IF an Applier raises THEN THE backend SHALL keep that key's previous value,
   SHALL log the exception once, and SHALL go on with the other keys.

### Requirement 4: Live keys and the no-change case are preserved

**User Story:** As an operator, I want keys that already update live, and
deployments that do not change LocalServer's configuration, to behave exactly
as today.

#### Acceptance Criteria

1. THE backend SHALL CONTINUE TO read `LocalLoginEnabled` and
   `ModelLoadSelfHealEnabled` live, with their current timing, and
   `accessControl` SHALL CONTINUE TO be applied by the nucleus; a change to
   any of them SHALL cause neither an Applier call nor an Explicit_Restart.
2. WHEN a configuration-update notification arrives and no Startup_Only_Key
   value differs from the value applied last THEN THE backend SHALL do
   nothing beyond one DEBUG line: no Applier call and no restart.
3. WHEN a deployment changes LocalServer's component version THEN Greengrass
   SHALL CONTINUE TO restart LocalServer as today, and the new process SHALL
   read every key at its start.
4. THE dependency type of `aws.greengrass.Cli` SHALL stay SOFT in every
   LocalServer recipe and publish path.

### Requirement 5: Observability of the applied configuration

**User Story:** As an operator, I want to see which configuration the backend
runs with, so that I can tell whether a deployment's change took effect.

#### Acceptance Criteria

1. THE backend SHALL expose, in an existing system endpoint's response or a
   new read-only endpoint behind the existing authorization, for each
   Startup_Only_Key: whether it is set, a fingerprint of the applied value
   (never the value of a key that can hold credentials), when it was applied,
   and how (at start, in place, or by an Explicit_Restart).
2. THE backend SHALL persist the reason for its latest Explicit_Restart on the
   host volume, so that the process that starts after it can log one INFO line
   naming the keys and the reason.

### Requirement 6: Verification

**User Story:** As a maintainer, I want the change verified on real hardware,
so that the subscription and the restart behave as designed under Greengrass.

#### Acceptance Criteria

1. THE change SHALL be built and deployed to a JP5 device (the MIC-730) and
   verified there: a deployment that merges only a new `workflowTuning` is
   applied in place within 60 s; a deployment that merges only a key without
   an Applier leads to one logged Explicit_Restart; a deployment that changes
   nothing leads to neither; a run in progress is never cut off.
2. THE other arches (JP6, JP7, x86) SHALL be verified only with the owner's
   go for each device, and SHALL otherwise be recorded as pending.
