# Implementation Plan

## Overview

Make a workflow bound to the virtual Static_Image_Camera runnable on device.
Two sequential defects, both fixed here (design.md Decisions 1-4):

1. **`workflow_engine/runtime.py:292`** — the production `inventory_provider()`
   closure calls `build_inventory(image_sources, snapshot)` without
   `static_image_pinned`, so the virtual entry is never in the workflow
   inventory and `resolve_bindings` marks the artifact set
   `invalid: missing camera source static-image-camera` every 5 s. Fix: the
   guarded `get_store().status()` block from `camera_sync/agent.py:911-938`,
   passing `static_image_pinned` + `static_image_metadata` only.
2. **`workflow_engine/camera_binding.py:223-237`** —
   `_resolved_parameter_values` projects only `params`, and the virtual entry's
   `params` is empty by design with its identity under
   `capabilities.staticImage`, so a resolved static binding yields an empty
   assignment, `aravis_feed._effective_values` discards the node's rendered
   `camera_id`, and every run fails `no camera id`. Fix: a capabilities fallback
   emitting `camera_id` + `cameraId`, mirroring the Portal's `cameraIdValue()`.

Defect 1 masks defect 2, so the exploration test must record both failures and
the fix legs land in that order.

**Hard constraints.** The virtual entry's shape — `params: {}` included — is the
shipped contract pinned by
`static-image-camera-binding-and-pin-discoverability` Reqs 3.5/3.17 and by
`test_property_static_camera_dedup_preservation.py`: populating `params` is not
an acceptable fix. `aravis_feed._effective_values`' precedence is pinned by
`test_property_aravis_feed_plan_precedence.py` and is not touched. The pin-store
call MUST be guarded: `get_store()` raises `KeyError` without
`COMPONENT_WORK_PATH`, and `watcher._local_inventory()` turns a provider
exception into `{}`, which would invalidate every camera binding on the device.

**Test commands.** Device suites from the repo root:
`~/.venvs/dda-edge-tests/bin/python -m pytest test/backend-test/<path> -q -p no:cacheprovider`
(confirm the interpreter in task 1.1; the portal venv is
`~/.venvs/dda-portal-tests`). Guard suite before any build, per
`.kiro/steering/builds.md`.

**On-device rule.** `src/backend/` code. Not done until built and verified on
real JP7 hardware (task 6). Unit and container tests are necessary but not
sufficient.

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "description": "Confirm the bug and pin today's behaviour before changing anything.", "tasks": ["1.1", "1.2", "1.3"] },
    { "wave": 2, "description": "Fix leg 1 (provider) and leg 2 (capabilities fallback).", "tasks": ["2.1", "2.2"] },
    { "wave": 3, "description": "Provider-level test that would have caught this, plus targeted unit coverage.", "tasks": ["3.1", "3.2"] },
    { "wave": 4, "description": "Gates: full device suites, guard suite, no baseline drift.", "tasks": ["4.1", "4.2"] },
    { "wave": 5, "description": "USER ACTION: build the JP7 LocalServer component.", "tasks": ["5"] },
    { "wave": 6, "description": "USER ACTION: hardware verification on jetson-thor1.", "tasks": ["6"] }
  ]
}
```

## Tasks

- [x] 1. Confirm the bug, pin the current behaviour
  - [x] 1.1 Establish the device test environment: identify the interpreter that
    runs `test/backend-test/workflow_engine` and `test/backend-test/camera_sync`
    green today, and record the exact command plus the current pass/skip counts
    for `test_workflow_camera_binding.py`,
    `test_workflow_watcher_binding_behavior.py`,
    `test_property_aravis_feed_plan_precedence.py`,
    `test_property_static_camera_dedup_preservation.py` and
    `test_property_pin_inventory.py`. These counts are the before-picture every
    later task is compared against.
    - _Requirements: —_
  - [x] 1.2 Exploration suite (MUST FAIL on unfixed code):
    `test/backend-test/workflow_engine/test_static_camera_binding_exploration.py`.
    Two cases, one per defect, both tagged with the bugfix.md property they
    prove:
    (a) **defect 1** — with a temp-dir `StaticImageStore` holding a pinned image
    and `server_setup` / `SessionLocal` substituted, the production
    `runtime._camera_binding_dependencies()` provider's output does NOT contain
    `static-image-camera`, and `resolve_bindings` for a document whose camera
    binding names it returns `status=INVALID` with
    `errors == ("missing camera source static-image-camera",)`;
    (b) **defect 2** — with the inventory stubbed to include the real virtual
    entry (built via `build_inventory(..., static_image_pinned=True)`, so the
    shape is the production one), `resolve_bindings` resolves but
    `aravis_feed.plan_aravis_feeds` raises `AravisFeedError` whose message
    contains "no camera id".
    Record both failure texts verbatim in this task's outcome — (b) is the
    evidence that the one-line fix is insufficient.
    - _Requirements: 1.1-1.8, Property 1_
  - [x] 1.3 Preservation suite (MUST PASS on unfixed code), written
    observation-first:
    `test/backend-test/workflow_engine/test_static_camera_binding_preservation.py`.
    For a physical Aravis entry (populated `params.cameraId`), a V4L2 entry, a
    CSI entry, an `override` binding and an unbound binding point: the resolved
    values, the assignment dict and the `plan_aravis_feeds` output are exactly
    what `F` produces (captured as explicit expected values, not
    self-referential). Plus: a binding naming a genuinely absent source still
    yields `INVALID` with `missing camera source {id}` (3.6), and a configured
    Image_Source whose `cameraId` is `static-image-camera` still resolves with a
    populated `params.cameraId` (3.4 — the RF-DETR workaround path).
    - _Requirements: 3.4, 3.5, 3.6, Property 2_

- [x] 2. The two fix legs
  - [x] 2.1 **Leg 1 — the provider.** `src/backend/workflow_engine/runtime.py`,
    `inventory_provider()`: add the function-local
    `from utils.static_image_camera import get_store`, the guarded
    `status()` block (design.md Decision 2 — `except Exception` +
    `logger.exception`, never propagating), and pass
    `static_image_pinned=` / `static_image_metadata=` to `build_inventory`.
    Do NOT pass `static_image_absent_since` (Decision 3); the docstring records
    why — the value is shadow-merge-specific, the local resolver ignores
    `absent`, and inventing one would put a meaningless timestamp in a typed
    field. Exploration case (a) now passes; case (b) still fails.
    - _Requirements: 2.1, 2.2, 2.5, 2.6_
  - [x] 2.2 **Leg 2 — the capabilities fallback.**
    `src/backend/workflow_engine/camera_binding.py`,
    `_resolved_parameter_values`: after the existing `params` projection, when no
    camera-id key resolved, read the camera id from
    `capabilities.staticImage.id` and emit BOTH `camera_id` and `cameraId` (the
    same pair `_PARAM_ALIASES` yields for a populated `params.cameraId`), so a
    static assignment is shape-indistinguishable from a physical one and every
    downstream reader works unchanged. Keyed on the capability family, not on the
    literal `static-image-camera` string. Comment cites the Portal's
    `cameraIdValue()` fix (`static-image-camera-binding-and-pin-discoverability`
    Reqs 2.1-2.4) and records that the entry's `params` is deliberately left
    empty per Reqs 3.5/3.17. `aravis_feed.py` is NOT modified. Exploration case
    (b) now passes; task 1.3 still passes untouched.
    - _Requirements: 2.3, 2.4, 3.2, 3.5_

- [x] 3. The coverage that was missing
  - [x] 3.1 `test/backend-test/workflow_engine/test_inventory_provider.py` —
    bugfix.md Property 3, the test that would have caught this bug. Drives the
    REAL `_camera_binding_dependencies()` closure (substituting `server_setup`,
    `SessionLocal` and a temp-dir store; no injected lambda): pinned → the
    virtual entry is present with the production shape; unpinned → absent, and
    per Decision 3 no entry at all; store raising (`COMPONENT_WORK_PATH` unset)
    → the inventory still contains the configured/discovered sources and is NOT
    `{}` (2.5). Assert the `build_inventory` call's keyword arguments directly,
    so a future kwarg omission fails here.
    - _Requirements: 2.1, 2.5, 2.7, Property 3_
  - [x] 3.2 Extend two existing suites rather than duplicating them:
    `test/backend-test/camera_sync/test_property_pin_inventory.py` gains the
    workflow-provider leg (presence tracks pin state on BOTH inventories, agent
    and workflow), and
    `test/backend-test/workflow_engine/test_workflow_camera_binding.py` gains the
    `_resolved_parameter_values` capabilities-fallback unit (virtual entry →
    `camera_id`/`cameraId`; physical entry → byte-identical to today; entry with
    neither → unchanged empty result).
    - _Requirements: 2.1, 2.4, Property 1, Property 2_

- [x] 4. Gates
  - [x] 4.1 Full `test/backend-test/workflow_engine` and
    `test/backend-test/camera_sync` green, at or better than task 1.1's counts.
    Explicitly confirm untouched and passing:
    `test_property_static_camera_dedup_preservation.py`,
    `test_property_aravis_feed_plan_precedence.py`,
    `test_property_registration_reevaluation.py`,
    `test_workflow_watcher_binding_behavior.py`.
    - _Requirements: 3.1-3.10, Property 2_
  - [x] 4.2 Security preservation gate at or better than baseline, and confirm
    no rebaseline is owed: neither changed file is pinned in
    `test/backend-test/security/baselines/` (verify by grep, do not assume). Run
    the two out-of-scope guards; move `edge-cv-portal/infrastructure/cdk.out`
    aside first if a portal deploy has regenerated it.
    - _Requirements: —_

- [x] 5. USER ACTION — build the JP7 LocalServer component
  - Per `.kiro/steering/builds.md`: confirm no other component build is running
    (`pgrep -af "gdk component build"`, `pgrep -af build-custom.sh`), move
    `cdk.out` aside, run the guard suite green FIRST (it runs late in the build,
    so a stale baseline wastes the whole ~1-2 h), then build
    `aws.edgeml.dda.LocalServer.arm64JP7` — one target at a time, never
    concurrently with a portal deploy. Log to `.gdk_build_jp7.log`.
  - Only JP7 is strictly required: the bug is arch-independent but was observed
    on JP7 and that is the device available for verification. If JP5/JP6 are
    built too, each needs its own hardware pass.
    - _Requirements: 2.8_

- [x] 6. USER ACTION — hardware verification on `jetson-thor1`
  - Deploy the built component, then, with an image pinned through the portal
    pin API:
    (a) deploy a workflow whose camera node is bound **by the
    `static-image-camera` identifier itself**, not via a wrapping Image_Source,
    and confirm the backend logs `registered as runnable` with NO
    `missing camera source` line;
    (b) trigger it repeatedly and confirm real inference output, with the backend
    healthy and no container restart for a sustained period — not just at
    startup;
    (c) confirm Decision 3's chosen unpinned behaviour: unpin → the registration
    goes invalid with a reason naming the camera; re-pin → it returns to runnable
    without a redeploy;
    (d) confirm the `cfg-my6j3zx1` wrapping-Image_Source binding still resolves
    (3.4).
  - Record the result in this spec as `verification-notes.md`, following the
    precedent in
    `.kiro/specs/static-image-camera-binding-and-pin-discoverability/`, and note
    in `docs/detection-training-gap.md` that the RF-DETR workaround is no longer
    required.
    - _Requirements: 2.3, 2.8, 3.4_
