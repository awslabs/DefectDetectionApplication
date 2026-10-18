# Implementation Plan: localserver-config-live-update

Not implemented in the `triton-model-cleanup-wedge` run: this spec was written
there as documents only, and every task below is open.

## Overview

Add a `Config_Update_Watcher` to the LocalServer backend that subscribes to the
component's own configuration, re-reads it after a debounced notification, and
applies each changed startup-only key through the Applier of its owning
module, or through one explicit, logged, rate-limited backend restart
(design.md). It depends on `triton-model-cleanup-wedge`'s `run_activity` and
restart path, so it starts only after that change is merged.

Build and device rules are those of `.kiro/steering/builds.md`: one build at a
time machine-wide, preservation pins rebaselined before the build, JP5 first,
and every on-device change built, deployed and verified on a device of the
matching arch before it is committed.

## Tasks

- [ ] 1. Re-check the inventory
  - [ ] 1.1 Re-run the greps of requirements.md 1.2 under `src/backend` and
    update the inventory table with any key, reader or line that changed
    - _Requirements: 1.1, 1.2, 1.3_

- [ ] 2. The watcher
  - [ ] 2.1 Write the unit tests of design.md "Testing Strategy" for the
    watcher first, with a fake IPC client (events, `GetConfiguration`
    answers, a stream that closes)
    - _Requirements: 2.1, 2.6, 3.1, 4.1, 4.2_
  - [ ] 2.2 Add `src/backend/utils/config_update_watcher.py`: the
    subscription on `key_path=[]`, the 2 s debounce, the single re-read
    through `get_component_config`, the comparison with the ignore list of
    live keys, and the polling fallback with the 5 min re-subscription
    - _Requirements: 2.1, 2.6, 3.1, 4.1, 4.2_
  - [ ] 2.3 Start the watcher from `utils/server_setup.py` after the existing
    readers, contained so that a failure leaves LocalServer as today
    - _Requirements: 4.3_

- [ ] 3. Appliers
  - [ ] 3.1 `workflowTuning`: replace the sample exporter in place; answer
    `needs_restart` when export is turned on or off
    - _Requirements: 2.2, 2.3, 3.2_
  - [ ] 3.2 `StationName`, `SoftwareVersion`, `WebuxUrl`, `TenantID`,
    `DeviceID`: clear the `DefectDetectionConfig` cache on every instance
    - _Requirements: 2.2_
  - [ ] 3.3 `CameraDiscoveryIntervalSeconds`: `CameraDiscovery.set_interval`
    with the existing validation, and the wake event in `_run_loop`
    - _Requirements: 2.2, 3.2_
  - [ ] 3.4 One test file per Applier, plus the preservation suites of the
    touched modules
    - _Requirements: 2.2, 3.2, 3.3, 4.1_

- [ ] 4. Explicit restart
  - [ ] 4.1 The restart through `run_activity` that waits for runs and never
    interrupts one, the persisted rate limit, the WARNING before the signal,
    the INFO after the restart, and the ERROR when the limit is reached
    - _Requirements: 2.3, 2.4, 2.5, 5.2_
  - [ ] 4.2 Tests across a simulated restart (a new watcher reading the same
    record) and with a registered run
    - _Requirements: 2.4, 2.5_

- [ ] 5. Observability
  - [ ] 5.1 `GET /system/configuration-status` behind the existing
    authorization, with fingerprints instead of values
    - _Requirements: 5.1_

- [ ] 6. Build and verify
  - [ ] 6.1 Run the pre-build gate of `.kiro/steering/builds.md`, then build
    and publish the JP5 LocalServer
    - _Requirements: 6.1_
  - [ ] 6.2 On the MIC-730 (JP5), with the owner's go: the in-place
    `workflowTuning` change, a local configuration merge, the restart for a
    key without an Applier, the no-change deployment, and a run during the
    restart wait
    - _Requirements: 2.1, 2.5, 4.2, 6.1_
  - [ ] 6.3 Other arches only with the owner's go per device; otherwise record
    them as pending
    - _Requirements: 6.2_

- [ ] 7. Retire the interim procedure
  - [ ] 7.1 Replace the "Interim behavior change from the SOFT CLI dependency"
    section of `.kiro/specs/triton-model-cleanup-wedge/bugfix.md` as
    design.md "Retiring the Interim Procedure" describes, with a dated
    revision-history entry
    - _Requirements: 2.1_
