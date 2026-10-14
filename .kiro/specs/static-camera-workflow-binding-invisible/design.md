# Static_Image_Camera Invisible To Workflow Binding — Bugfix Design

## Overview

Two independent defects make a workflow bound to the virtual
Static_Image_Camera permanently unrunnable on device. Fixing either one alone
leaves the feature broken, in a different place:

| # | Defect | Symptom | Fix leg |
|---|---|---|---|
| 1 | `workflow_engine/runtime.py:292` calls `build_inventory(image_sources, snapshot)` without `static_image_pinned`, so the virtual entry is never in the workflow inventory | artifact set `invalid: missing camera source static-image-camera`, re-logged every 5 s, triggers never armed | pass the pin state, guarded, mirroring `camera_sync/agent.py:911-938` |
| 2 | the virtual entry carries `params: {}` with its identity under `capabilities.staticImage`, and `_resolved_parameter_values` projects only `params` | registration goes green, then **every run** fails `Aravis camera source '<node>': no camera id` | capabilities fallback in `_resolved_parameter_values`, mirroring the Portal's `cameraIdValue()` |

Both legs are small and local. The risk in this bugfix is not the change size,
it is the **blast radius on adjacent pinned contracts**: the entry's shape is
pinned by `static-image-camera-binding-and-pin-discoverability` Reqs 3.5/3.17,
and the assignment-vs-rendered parameter precedence is pinned by
`test_property_aravis_feed_plan_precedence.py`. The design below is chosen to
touch neither.

## Glossary

- **Virtual entry** — the single `CameraSourceState` for the Static_Image_Camera
  that `build_inventory` appends (`inventory.py:391-410`): fixed id
  `static-image-camera`, `type: StaticImage`, `origin: edge-discovered`,
  `params: {}`, identity + pin metadata under `capabilities.staticImage`.
- **Workflow inventory** — what the workflow engine's production
  `inventory_provider()` closure returns (`runtime.py:276-292`), consumed only
  by `resolve_bindings` through `watcher._local_inventory()`.
- **Agent inventory** — what the Edge_Sync_Agent builds for the
  `dda-camera-registry` shadow (`agent.py:899-938`). Correct today; out of
  scope.
- **Pin store** — the `StaticImageStore` singleton from
  `utils.static_image_camera.get_store()`.
- **Assignment** — the `{cameraSourceId, params}` dict `resolve_bindings` puts
  in `ResolutionResult.aravis_assignments` for an Aravis binding point.

## Bug Details

### Bug condition

A Pinned_Image exists, and some camera binding point of a registered workflow
names `cameraSourceId: "static-image-camera"`. See bugfix.md
`isBugCondition(W)`.

### Why the two defects are sequential, not alternative

`resolve_bindings` must find the entry before it can project its parameters, so
defect 1 masks defect 2 entirely. That ordering dictates the task order and the
exploration-test design: the Property 1 exploration test must record **both**
failure modes, the second one reached by stubbing the inventory past the first.

### What already works, and is the reason the bug looks impossible

At the same instant the watcher says the camera is missing:

- `GET /cameras` lists it — `aravis_functions.py:113-118` appends
  `Camera(**STATIC_IMAGE_CAMERA_IDENTITY)` straight from `get_store().is_pinned()`.
- `dda-camera-registry` reports it present — the agent passes the kwargs.
- `camera_manager` would serve its frames — grab short-circuits on the id
  before touching the Aravis bus (`camera_manager.py:806-812`).
- The Portal offers and delivers the binding (`camera_bindings_delivered: true`).

Only the workflow provider is wrong. Three independent surfaces consult the pin
store directly; the fourth forgot to.

## Expected Behavior

bugfix.md Requirements 2.1-2.8. In one line: a workflow bound to the pinned
static camera registers as runnable and executes to completion through the same
Aravis feed path a physical camera uses.

### Preservation Requirements

bugfix.md 3.1-3.10. The three that constrain the design most:

- **3.2** the virtual entry's shape, `params: {}` included, is the shipped
  contract — so the fix may not populate `params`.
- **3.5** physical Aravis bindings keep their exact resolved values, assignment
  shape and feed plan.
- **3.6** a genuinely missing camera source still marks the set invalid.

## Hypothesized Root Cause

Not hypothesized — confirmed by reading both call sites. `runtime.py`'s provider
is a near-copy of the agent's `_load_inventory` that predates the
static-camera kwargs and was never updated when
`cloud-static-camera-provisioning` added them. The copy shares the accessor and
the snapshot source; it differs only in the three arguments and in lacking the
`absentSince` bookkeeping (which is shadow-merge-specific and correctly absent).

Defect 2 is the device-side survivor of a fix applied only to the Portal:
`static-image-camera-binding-and-pin-discoverability` Defect 1 gave the Portal's
`cameraIdValue()` a `capabilities.staticImage` fallback because the entry's
`params` is empty. The two device readers with the same assumption
(`_resolved_parameter_values`, `aravis_feed._camera_id`) were never touched.

## Design Decisions

### Decision 1: Fix the provider, not `build_inventory`

`build_inventory`'s defaults are correct — it is a pure merge whose caller owns
the pin state, and the agent depends on the absent-entry branch. The provider is
the only wrong caller, so the change is confined to `runtime.py`'s closure.

### Decision 2: Consult the pin store with `status()`, guarded exactly as the agent does

```python
static_image_pinned = False
static_image_metadata = None
try:
    status = get_store().status()
    static_image_pinned = bool(status.get("pinned"))
    static_image_metadata = status.get("metadata")
except Exception:      # noqa: BLE001 - a store failure must not empty the inventory
    logger.exception(
        "Static image pin state could not be read; resolving camera bindings "
        "without the static camera entry"
    )
```

`status()` and `is_pinned()` cost the same (both call `_inspect_locked()`), and
the entry wants the metadata anyway, so `status()` is the correct call and keeps
the provider symmetric with `agent.py:915-917`.

**The guard is load-bearing, not defensive boilerplate.** `get_store()`
constructs `StaticImageStore()`, whose `__init__` does
`os.environ["COMPONENT_WORK_PATH"]` and therefore raises `KeyError` off-device.
`watcher._local_inventory()` converts any provider exception into `{}`
(`watcher.py:406-417`), so an unguarded raise would mark **every** camera
binding on the device invalid — converting a static-camera bug into a total
outage. The import is function-local, matching the deferred-import discipline
the closure already documents.

Cost: one `os.stat` plus a small `json.load` on a warm decode cache, against a
provider call that already opens a DB session and lists every Image_Source. The
cache is process-wide and shared with `/cameras`, the agent and the camera
manager, so it is warm in practice. A cold cache costs one Pillow decode. No
memoization in v1 — if it is ever needed, the lever is memoizing on the store's
own `(ino, mtime, size)` key, not skipping the call.

### Decision 3: Surface the entry only while pinned (bugfix.md 2.6 choice)

bugfix.md 2.6 requires an explicit, recorded choice for the unpinned case.
**Chosen: pass `static_image_pinned` + `static_image_metadata` only; do not pass
`static_image_absent_since`.** Unpinning therefore flips the registration to
invalid within one poll tick, with the existing `missing camera source` reason.

Why, over the alternative of always surfacing the entry (present when pinned,
absent otherwise):

1. **No fabricated timestamp.** `static_image_absent_since` is epoch-ms with a
   specific meaning — when absence was first established, derived from the pin
   worker's marker and stabilized across restarts so the *shadow* does not churn
   (`agent.py:940-989`). The local resolver never reads `absent` or
   `absentSince`, so the provider would have to invent a value that means
   nothing. Putting a lie in a typed field to satisfy a consumer that ignores it
   is worse than the behaviour it buys.
2. **Smaller observable delta.** This bugfix should change exactly one thing:
   the pinned case starts working. Option B additionally changes what an
   *unpinned* device reports to a consumer that today sees nothing.
3. **The invalid state is honest and self-healing.** "missing camera source
   static-image-camera" on a device with no pinned image is true, and the
   existing re-resolution hooks flip the registration back to registered on the
   next pass once an image is pinned (`test_property_registration_reevaluation.py`
   pins that behaviour).

Recorded tension, for the record rather than as a defect: this differs from an
absent *physical* camera, which still resolves (because `resolve_bindings` never
inspects `absent`) and fails at grab time, and it means an unpin cancels a
registration rather than failing a run the way
`static-image-camera-source` Reqs 4.6/5.6 describe. Those criteria are about a
run that references a removed camera, and `camera_manager` already raises
`StaticImageUnavailableError` naming the camera for exactly that case — so the
run-level contract is met on the path it describes. If operators later want the
registration to survive an unpin, the change is to pass a real `absentSince`
sourced from the pin worker marker, which is a follow-up with its own
observable behaviour, not a silent widening of this one.

### Decision 4: Capabilities fallback in `_resolved_parameter_values`, not merge precedence in `_effective_values`

Two places could fix defect 2:

- **(a) `camera_binding._resolved_parameter_values`** — when `params` yields no
  camera id, fall back to `capabilities.staticImage.id`. Narrow: only entries
  whose `params` lack a camera id and whose capabilities supply one are affected,
  which today is exactly the virtual entry. Physical Aravis entries always carry
  `params.cameraId`, so their resolved values are byte-identical (3.5).
- **(b) `aravis_feed._effective_values`** — merge the assignment's params *over*
  the rendered parameters instead of replacing them. Broader: changes precedence
  for **every** Aravis binding, and is pinned by
  `test_property_aravis_feed_plan_precedence.py`.

**Chosen: (a).** It mirrors the Portal fix for the identical defect
(`static-image-camera-binding-and-pin-discoverability` Reqs 2.1-2.4), keeps the
device and Portal reading the entry the same way, and needs no pinned-test
repoint. (b) is recorded as rejected: it would make a correct-by-accident
outcome depend on precedence semantics that other bindings rely on, for no extra
coverage.

Shape of (a): the fallback emits **both** `camera_id` and `cameraId` — the same
pair `_PARAM_ALIASES` produces for a populated `params.cameraId` — so a static
assignment is indistinguishable in shape from a physical one and every existing
downstream reader (`aravis_feed._camera_id` checks `("camera_id", "cameraId")`)
works unchanged. It is keyed on the capability family, not on the literal id, so
it does not hard-code `static-image-camera` into the binding resolver.

### Decision 5: No change to the entry, the agent, or the dedup

Stated explicitly because all three are tempting and all three are wrong:
populating `params.cameraId` on the virtual entry would satisfy defect 2 in one
line and is **forbidden** by Reqs 3.5/3.17 and enforced by
`test_property_static_camera_dedup_preservation.py`; re-including the
aravis-enumerated duplicate would satisfy defect 1 by accident and undo Reqs
2.7/2.8; and the agent is already correct.

### Decision 6: One new test drives the production closure

The bug shipped because no test exercises the real
`_camera_binding_dependencies()` provider — every watcher test injects a lambda,
every `build_inventory` test passes explicit kwargs. bugfix.md Property 3
requires a test that substitutes `server_setup`, `SessionLocal` and a
temp-directory store, then asserts the **closure's** output. This is the only
new test that would have caught the bug, and it is what stops the same omission
recurring for the next kwarg.

## Correctness Properties

Per bugfix.md. Mapping to the artifacts that prove them:

| Property | Statement | Artifact |
|---|---|---|
| 1 | a workflow bound to the pinned static camera registers AND plans its feed | exploration suite (must fail twice on `F`), then fix-check |
| 2 | every non-static binding, and the agent inventory, byte-identical | new preservation suite + existing `test_property_static_camera_dedup_preservation.py`, `test_property_aravis_feed_plan_precedence.py` untouched |
| 3 | the production provider passes the pin state and survives a store failure | new provider suite (Decision 6) |

## Fix Implementation

### Changes Required

1. **`src/backend/workflow_engine/runtime.py`** — `inventory_provider()`:
   function-local `from utils.static_image_camera import get_store`, the guarded
   `status()` block of Decision 2, and `build_inventory(image_sources, snapshot,
   static_image_pinned=..., static_image_metadata=...)`. Docstring records why
   `absentSince` is deliberately not passed (Decision 3).
2. **`src/backend/workflow_engine/camera_binding.py`** —
   `_resolved_parameter_values`: after the existing `params` projection, when no
   camera-id key resolved, read `capabilities.staticImage.id` and emit
   `camera_id` + `cameraId` (Decision 4). Comment cites the Portal's
   `cameraIdValue()` and Reqs 3.5/3.17 as the reason the entry itself is not
   changed.

No other production file changes. No new module, no new dependency, no schema or
shadow change, no IAM change, and nothing under `edge-cv-portal/`.

### Not changed

`camera_sync/inventory.py`, `camera_sync/agent.py`,
`utils/static_image_camera.py`, `aravis_feed.py`, the Portal, the pin API, and
every V4L2 / CSI / adapter binding path.

## Testing Strategy

Bugfix methodology order: exploration (fails on `F`) → preservation
(observation-first, passes on `F`) → fix → both pass → hardware.

- **Exploration**, new `test/backend-test/workflow_engine/test_static_camera_binding_exploration.py`:
  (i) the production provider omits the entry while pinned; (ii) with the
  inventory stubbed past, `plan_aravis_feeds` raises "no camera id". Both must
  fail on `F` and the failure text is recorded in tasks.md.
- **Preservation**, new
  `test/backend-test/workflow_engine/test_static_camera_binding_preservation.py`:
  resolved values / assignment shape / feed plan for physical Aravis, V4L2, CSI
  and override bindings identical across `F`/`F'`; a genuinely missing source
  still invalid (3.6). Must pass on `F`.
- **Provider**, new
  `test/backend-test/workflow_engine/test_inventory_provider.py` — Property 3,
  including the `COMPONENT_WORK_PATH`-unset case asserting a non-empty inventory
  (2.5).
- **Extend** `test/backend-test/camera_sync/test_property_pin_inventory.py` with
  the workflow-provider leg, and
  `test/backend-test/workflow_engine/test_workflow_camera_binding.py` with the
  capabilities-fallback unit.
- **Untouched and must stay green**:
  `test_property_static_camera_dedup_preservation.py`,
  `test_property_aravis_feed_plan_precedence.py`,
  `test_property_registration_reevaluation.py`,
  `test_workflow_watcher_binding_behavior.py`.

No preservation-tracked file is touched, so no security baseline rebaseline is
owed; the guard suite still runs before the build per `.kiro/steering/builds.md`.

## Hardware Verification

Required by bugfix.md 2.8 and `.kiro/steering/builds.md`; unit tests are not
sufficient. On JP7 `jetson-thor1` (where the bug was observed): pin an image,
deploy a workflow whose camera node is bound **by the `static-image-camera`
identifier itself** (not via a wrapping Image_Source), confirm it registers as
runnable with no `missing camera source` line, trigger it repeatedly and confirm
inference output with the backend staying healthy for a sustained period. Then
unpin and confirm Decision 3's chosen behaviour (registration invalid, reason
names the camera), re-pin and confirm it returns to runnable. The existing
`cfg-my6j3zx1` workaround binding must keep working (3.4).
