# Bugfix Requirements Document

## Introduction

A deployed workflow whose camera node is bound to the virtual
Static_Image_Camera is **permanently unrunnable on device**. The workflow
engine's watcher rejects the artifact set on every scan pass, roughly every
five seconds, with:

```
Workflow artifact set ae783ac8-3cf2-4baf-b242-a3bb284776a9:2 registered as
invalid: missing camera source static-image-camera [workflow_engine.watcher]
```

The binding is not malformed and the camera is not missing. At the moment of
that log line the device held a valid Pinned_Image, `GET /cameras` listed
`static-image-camera`, the `dda-camera-registry` shadow reported it
(`type: StaticImage`, `absent: false`), and the Portal had accepted the
binding and delivered it into the `dda-camera-bindings` shadow. Only the
workflow engine cannot see the camera.

Observed live on 2026-09-22 on `jetson-thor1` (JP7 Orin, LocalServer
`arm64JP7` 1.0.43) while verifying task 11 of
`.kiro/specs/rfdetr-training-and-transfer-learning/`. The RF-DETR
verification had to be completed through a workaround — binding the node to a
configured Image_Source (`cfg-my6j3zx1`) whose `cameraId` is
`static-image-camera`, which resolves normally — so the underlying defect was
recorded and left unfixed. Record:
`docs/detection-training-gap.md` §"On-device verification (2026-09-22)".

**Root cause chain (verified in code).** Two independent defects sit on the
same path, and fixing only the first moves the failure rather than removing
it.

- `src/backend/camera_sync/inventory.py:152-158` —
  `build_inventory(image_sources, discovery_result, static_image_pinned=False,
  static_image_metadata=None, static_image_absent_since=None)`. The virtual
  entry is appended only at `:350-354`, gated on `static_image_pinned` (or, for
  the absent variant, on `static_image_absent_since`). **A two-argument call
  can never produce a `static-image-camera` entry.**
- `src/backend/camera_sync/agent.py:911-938` — the Edge_Sync_Agent computes
  the pin state from a guarded `get_store().status()` and passes all three
  kwargs. This is why the registry shadow and the Portal are correct.
- `src/backend/workflow_engine/runtime.py:286-292` — the workflow engine's
  `inventory_provider()` closure calls `build_inventory(image_sources,
  snapshot)` with **no static-image arguments**. The two providers are
  otherwise near-identical; the workflow one predates or missed the kwargs.
- `src/backend/workflow_engine/camera_binding.py:139-144` — `resolve_bindings`
  looks the `cameraSourceId` up in the normalized inventory and, on a plain
  dict miss, appends `"missing camera source {id}"`.
  `src/backend/workflow_engine/watcher.py:401-404` turns any such error into
  `STATUS_INVALID` with that reason, re-emitted on every scan pass
  (`DEFAULT_POLL_INTERVAL_SECONDS = 5.0`, `watcher.py:57`).

**The camera is doubly invisible on this path.** The dedicated entry is gated
off by the missing kwarg, *and* the aravis-enumerated static camera — which
previously leaked through as an incidental side effect — is now deliberately
excluded from the reported discovery entries by
`_is_static_image_aravis_camera` (`inventory.py:296-303`), the de-duplication
landed by `static-image-camera-binding-and-pin-discoverability` (its
Requirements 2.7, 2.8). Neither route reaches the workflow inventory.

**Second-order defect: resolving the entry is not sufficient.** The virtual
entry carries an empty `params` block by design
(`inventory.py:391-410`, identity under `capabilities.staticImage`), and
nothing under `src/backend/workflow_engine/` reads `capabilities` at all.
`_resolved_parameter_values` (`camera_binding.py:223-237`) projects **only**
`params`, so the Aravis assignment becomes `{"cameraSourceId":
"static-image-camera", "params": {}}`. `aravis_feed._effective_values`
(`aravis_feed.py:146-159`) then *prefers the assignment params over the
rendered parameters without merging* — and `{}` is a `Mapping`, so the node's
compiled-in `camera_id` is discarded, `_camera_id({})` returns `None`, and the
run dies at `aravis_feed.py:131-137`:

```
Aravis camera source '<node>': no camera id: neither the resolved binding nor
the rendered parameters carry a non-empty camera_id
```

So a `static_image_pinned=...` fix alone converts "invalid every 5 s" into
"registers fine, every run fails". This is the **device-side mirror of Defect
1** in `static-image-camera-binding-and-pin-discoverability`, where the Portal's
`cameraIdValue()` read only `params.cameraId` and was given a
`capabilities.staticImage` fallback — a fix applied to the frontend only. The
identical omission on the device was never addressed.

**Why no test caught it.** `grep -rn "_camera_binding_dependencies\|inventory_provider" test/`
finds no test that exercises the production provider: every watcher test
injects its own `inventory_provider` lambda, and every `build_inventory`
static-camera test calls the function directly with explicit kwargs. The one
closure that ships is untested. Likewise, the property suites that generate
inventories for binding resolution never generate a `StaticImage` entry with
empty `params`, which is why the second-order defect is also unexercised.

**Sibling coordination.**
- `.kiro/specs/static-image-camera-source/` (the base feature) is what this
  bug breaks most directly. Its Requirement 4.5 says a document authored for a
  physical Aravis camera whose binding resolves to the static identifier SHALL
  "plan and execute the run through the same camera binding and feed planning
  path used for physical Aravis camera identifiers" — the device never gets
  past binding resolution, and after a naive fix it fails inside exactly that
  feed-planning path. Requirement 4.2 (the run is fed a frame and "SHALL
  proceed to completion") is equally unmet. Requirement 2.4 is the reason the
  fixed identifier exists: so "Image_Source records **and workflow camera
  bindings** referencing the identifier stay valid".
- `.kiro/specs/cloud-static-camera-provisioning/` Requirement 6.4 and 6.6 are
  honored by the Portal — it offers the camera in the binding matrix and
  produces the binding through the existing mechanism — and then the device
  refuses it. That spec had no device-side acceptance criterion for the
  workflow binding path, which is the gap this bugfix closes.
- `.kiro/specs/static-image-camera-binding-and-pin-discoverability/` is
  directly adjacent: its Defect 1 is the same empty-`params` omission fixed on
  the Portal side, its Defect 3 removed the `arv-*` entry that used to satisfy
  this binding incidentally, and its Requirements 3.5 / 3.17 pin the entry's
  shape as the shipped, hardware-verified contract. Its Requirement 3.14
  asserts that de-duplicating the registry "invalidates no existing binding at
  runtime, because both duplicates carry that identical id string" — true for
  `camera_id` *string* equality, but **falsified in practice** for
  `cameraSourceId` bindings by this bug. Correcting the scope of that claim
  belongs in this spec.
- `.kiro/specs/rfdetr-training-and-transfer-learning/` task 11 is where the
  bug was found; its verification stands, via the workaround recorded there.

**Non-goals.** This spec does not change the shape of the virtual inventory
entry (Reqs 3.5/3.17 of the discoverability bugfix pin it, and
`test_property_static_camera_dedup_preservation.py` enforces it), does not
change the Edge_Sync_Agent's reporting or the `absentSince` shadow-merge
machinery, does not change the Portal, does not change the pin API or
`StaticImageStore`, and does not touch the V4L2 / CSI / adapter binding
families.

**On-device rule.** This is on-device code under `src/backend/`. Per
`.kiro/steering/builds.md`, the fix is not done until the affected LocalServer
component is built and deployed to real hardware and the feature is exercised
end to end with the backend staying healthy for a sustained period. JP7
(`jetson-thor1`) is the arch where the bug was observed and is mandatory; any
other arch the change touches must be verified too. Unit and container tests
are necessary but **not sufficient**.

## Bug Analysis

### Current Behavior (Defect)

**Defect 1 — the workflow engine's camera inventory omits the Static_Image_Camera**

1.1 WHEN the workflow engine builds its device-local Camera_Source inventory
THEN the system calls `build_inventory(image_sources, snapshot)` with no
static-image arguments (`workflow_engine/runtime.py:292`), so the virtual
`static-image-camera` entry is never appended regardless of pin state

1.2 WHEN a workflow's camera binding names `cameraSourceId:
"static-image-camera"` and a Pinned_Image exists THEN the system's
`resolve_bindings` misses the inventory lookup and records `"missing camera
source static-image-camera"` (`camera_binding.py:139-144`)

1.3 WHEN that error reaches the watcher THEN the system marks the artifact set
`STATUS_INVALID` with that reason (`watcher.py:401-404`), refuses to arm its
triggers, and re-emits the same error on every scan pass — approximately every
five seconds, indefinitely

1.4 WHEN the same camera is queried through any other device surface THEN the
system reports it as present — `GET /cameras` appends it from an independent
path (`edge_ml1_p_camera_management/aravis_functions.py:113-118`, gated on
`get_store().is_pinned()`), the Edge_Sync_Agent reports it into
`dda-camera-registry` (`agent.py:911-938`), and the camera-manager grab
short-circuits serve it — so the device contradicts itself and the operator has
no way to tell which surface is wrong

1.5 WHEN the Portal builds the Camera_Binding_Matrix THEN the system offers the
Static_Image_Camera as a bindable source and delivers the binding successfully
(`camera_bindings_delivered: true`), giving no indication that the target
device will refuse it

**Defect 2 — a resolved static binding still cannot plan a frame feed**

1.6 WHEN a binding resolves to the virtual entry THEN the system projects only
its `params` into the resolved values (`camera_binding.py:223-237`), and the
entry's `params` is empty by design, so the Aravis assignment carries
`{"params": {}}` and the entry's identity — which lives under
`capabilities.staticImage` — is discarded

1.7 WHEN `aravis_feed.plan_aravis_feeds` computes the effective values THEN the
system prefers the assignment's `params` over the node's rendered parameters
*without merging* (`aravis_feed.py:146-159`), so an empty assignment discards
the compiled-in `camera_id` rather than falling back to it

1.8 WHEN the effective values carry no camera id THEN the system raises
`AravisFeedError(node_id, "no camera id: neither the resolved binding nor the
rendered parameters carry a non-empty camera_id")` (`aravis_feed.py:131-137`)
and the run fails — so fixing Defect 1 alone relocates the failure from
registration time to run time

**Defect 3 — the production inventory provider is untested**

1.9 WHEN the test suite runs THEN the system's real
`runtime._camera_binding_dependencies()` / `inventory_provider()` closure is
never exercised: every watcher and binding-store test injects its own
`inventory_provider` lambda, and every `build_inventory` static-camera test
calls the function directly with explicit kwargs — so no test observes the
argument list that ships

1.10 WHEN the binding-resolution property suites generate inventories THEN the
system is never presented with a `StaticImage` entry carrying empty `params`
(the suites' own docstrings state `cameraId` is always present), so Defect 2 is
unexercised even though both halves of it are pure functions

### Expected Behavior (Correct)

2.1 WHEN the workflow engine builds its device-local inventory AND a
Pinned_Image exists THEN the system SHALL include the virtual
`static-image-camera` entry, with the same fixed identity the Edge_Sync_Agent
reports

2.2 WHEN a workflow camera binding names `static-image-camera` AND a
Pinned_Image exists THEN the system SHALL resolve the binding and register the
artifact set as runnable, arming its triggers

2.3 WHEN such a workflow executes THEN the system SHALL plan and execute the
Aravis frame feed through the same path a physical Aravis camera identifier
uses, feeding the Pinned_Image, and the run SHALL proceed to completion
(`static-image-camera-source` Requirements 4.2, 4.5)

2.4 WHEN a binding resolves to an inventory entry whose `params` carry no
camera id but whose `capabilities` do THEN the system SHALL resolve the camera
id from those capabilities — the device-side counterpart of the Portal's
`cameraIdValue()` fallback (`static-image-camera-binding-and-pin-discoverability`
Requirements 2.1-2.4)

2.5 WHEN the pin store cannot be read THEN the system SHALL degrade exactly as
the Edge_Sync_Agent does — log and proceed without the static entry — and SHALL
NOT allow the failure to empty the whole inventory. `get_store()` raises
`KeyError` when `COMPONENT_WORK_PATH` is unset, and
`watcher._local_inventory()` converts any provider exception into `{}`
(`watcher.py:406-417`), which would invalidate **every** camera binding on the
device, so the call MUST be guarded at the provider

2.6 WHEN no Pinned_Image exists THEN the design SHALL make an explicit,
recorded choice for the workflow inventory between (a) omitting the entry, so
the registration flips to invalid within one poll tick, and (b) surfacing it in
a non-present form, so the registration stays valid and the failure surfaces at
run time. Option (b) matches how absent *physical* cameras already behave —
`resolve_bindings` never inspects `absent`, so an absent physical camera still
resolves and fails at grab time — and matches `static-image-camera-source`
Requirements 4.6 / 5.6, which specify that a run referencing a removed
Static_Image_Camera fails **that run** with an error naming the camera
(`camera_manager` already raises `StaticImageUnavailableError` for exactly
this). The two options differ observably, so the choice must be stated, not
implied

2.7 WHEN the fix is complete THEN the system SHALL carry a test that exercises
the **production** inventory provider — not an injected substitute — since that
is the only kind of test that would have caught this

2.8 WHEN the fix is verified THEN verification SHALL be on real hardware: a
JP7 device with a Pinned_Image, a workflow bound to `static-image-camera` by
that identifier (not via a wrapping Image_Source), registering as runnable and
executing to completion with correct inference output, and the backend staying
healthy for a sustained period (`.kiro/steering/builds.md`)

### Unchanged Behavior (Regression Prevention)

3.1 WHEN the Edge_Sync_Agent reports the camera registry THEN the system SHALL
CONTINUE TO behave byte-identically — the `absentSince` derivation, the
previously-reported records, the retirement bookkeeping and the shadow document
are all out of scope

3.2 WHEN the virtual inventory entry is built THEN its shape SHALL CONTINUE
unchanged: fixed id, `type: StaticImage`, `origin: edge-discovered`, **empty
`params`**, identity plus pin metadata under `capabilities.staticImage`,
`discovered: true`, and the absent variant identical but for
`absent`/`absentSince` (`static-image-camera-binding-and-pin-discoverability`
Requirements 3.5, 3.17, pinned by
`test_property_static_camera_dedup_preservation.py`). Populating `params` is
**not** an acceptable fix

3.3 WHEN the aravis-enumerated static camera appears in a discovery snapshot
THEN the system SHALL CONTINUE TO exclude it from the reported discovery
entries in every pin state (Requirements 2.7, 2.8 of that bugfix), so the
virtual entry remains the single registration

3.4 WHEN a configured Image_Source whose `cameraId` is `static-image-camera`
is present THEN the system SHALL CONTINUE TO merge it exactly as today
(Requirement 3.18), yielding a `cfg-{imageSourceId}` entry with a populated
`params.cameraId` — this is the path the RF-DETR verification used as a
workaround and it must keep working

3.5 WHEN a binding resolves to a **physical** Aravis camera THEN the resolved
values, the assignment shape and the feed plan SHALL CONTINUE unchanged. In
particular, any change to `_effective_values`' precedence between assignment
params and rendered parameters affects every Aravis binding and is pinned by
`test_property_aravis_feed_plan_precedence.py`; a capabilities fallback in
`_resolved_parameter_values` is the narrower option and SHALL be preferred
unless the design justifies otherwise

3.6 WHEN a binding names a camera source that genuinely does not exist THEN the
system SHALL CONTINUE TO mark the artifact set invalid with `"missing camera
source {id}"` and refuse its triggers — this bugfix removes a false positive,
not the check (`test_workflow_camera_binding.py::test_missing_camera_source_marks_invalid_with_reason`,
`test_workflow_watcher_binding_behavior.py::test_missing_camera_source_rejects_trigger`)

3.7 WHEN an invalid registration's inventory later gains the missing source
THEN the system SHALL CONTINUE TO re-resolve and flip it to registered on the
next pass, hook or delta (camera-registry-sync Requirement 10.4,
`test_property_registration_reevaluation.py`)

3.8 WHEN camera-binding wiring is unavailable THEN the system SHALL CONTINUE TO
degrade to an unwired watcher that registers documents with their compiled-in
values (`runtime.py:55-68`), never taking LocalServer down

3.9 WHEN `/cameras`, `/cameras/rescan`, `getCamera()` and the camera-manager
grab/status short-circuits are called THEN the system SHALL CONTINUE TO serve
the Static_Image_Camera through their existing independent paths, unchanged

3.10 WHEN the provider is called THEN it SHALL CONTINUE TO read Image_Sources
through the existing accessor read-only (camera-registry-sync Requirement 11.3)
and SHALL NOT introduce a new write, a new table, or a new shadow

### Bug Conditions and Properties

**Key definitions.** `F` is the current (unfixed) code; `F'` is the fixed
code. `pinned()` is true when the pin store holds a usable Pinned_Image.
`workflowInventory()` is the mapping the workflow engine's production
`inventory_provider()` returns. `resolve(B)` is `resolve_bindings` for a
document with binding set `B`. `plan(N)` is `plan_aravis_feeds` for camera node
`N`. `STATIC` is the fixed identifier `static-image-camera`.

#### Bug condition — a workflow bound to the pinned Static_Image_Camera

```pascal
FUNCTION isBugCondition(W)
  INPUT: W of type DeployedWorkflow
  OUTPUT: boolean

  RETURN pinned()
     AND EXISTS n IN cameraBindingPoints(W) :
           binding(W, n).cameraSourceId = STATIC
END FUNCTION
```

```pascal
// Property 1: Fix Checking - a workflow bound to the pinned static camera
// registers AND can plan its feed
FOR ALL W WHERE isBugCondition(W) DO
  ASSERT STATIC IN workflowInventory'()                       // 2.1
  ASSERT resolve'(bindings(W)).status = RESOLVED               // 2.2
  ASSERT STATIC NOT IN resolve'(bindings(W)).errors
  FOR ALL n WHERE binding(W, n).cameraSourceId = STATIC DO
    ASSERT plan'(n) SUCCEEDS                                   // 2.3, 2.4
    ASSERT cameraIdOf(plan'(n)) = STATIC                       // 2.4
  END FOR
END FOR
```

Property 1 must fail against `F` in **two distinct ways**, and the exploration
test must record both: the inventory assertion fails outright, and — with the
inventory assertion stubbed past — the `plan` assertion fails with "no camera
id". A fix that satisfies only the first is incomplete (1.6-1.8).

#### Preservation — every other camera source and the agent are untouched

```pascal
// Property 2: Preservation Checking - for all non-static inputs the fixed
// system behaves identically to the original
FOR ALL W WHERE NOT isBugCondition(W) DO
  ASSERT resolve(bindings(W)) = resolve'(bindings(W))          // 3.5, 3.6
  FOR ALL n IN cameraBindingPoints(W) DO
    ASSERT plan(n) = plan'(n)                                  // 3.5
  END FOR
END FOR

FOR ALL (sources, snapshot, pinState) DO
  ASSERT agentInventory(sources, snapshot, pinState)
       = agentInventory'(sources, snapshot, pinState)           // 3.1, 3.2, 3.3
  ASSERT entryShape'(STATIC) = entryShape(STATIC)               // 3.2
END FOR
```

#### Provider-level property — the shipped closure passes the pin state

```pascal
// Property 3: the production provider, not a substitute
FOR ALL pinState IN {pinned, unpinned} DO
  inv ← productionInventoryProvider'()                          // 2.7
  ASSERT pinState = pinned IMPLIES STATIC IN inv                // 2.1
  ASSERT storeRaises() IMPLIES inv ≠ {} AND STATIC NOT IN inv   // 2.5
END FOR
```

Property 3 is the one that would have caught this bug. It must drive the real
`_camera_binding_dependencies()` closure — with `server_setup`, `SessionLocal`
and a temp-directory store substituted — rather than any injected lambda.

**Note on exploration order (bugfix methodology).** The exploration tests for
Property 1 MUST be written and run against UNFIXED code first and are expected
to FAIL, recording both failure modes above. Preservation tests for Property 2
MUST be written observation-first against UNFIXED code and PASS before any fix
lands; `test_property_static_camera_dedup_preservation.py` already covers a
large part of it and must keep passing untouched. Final validation is on real
hardware per 2.8.
