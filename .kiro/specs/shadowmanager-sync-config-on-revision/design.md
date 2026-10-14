# ShadowManager Sync Config On Revision — Bugfix Design

## Overview

Every portal deployment REVISION ships `aws.greengrass.ShadowManager` bare
(or with a stale merge), permanently disarming the portal's synchronize
auto-include after a target's first revision. Verified fleet impact
(bugfix.md Incident Record, jetson-thor1): revision 2 (Aug 14) was the last
revision carrying a ShadowManager `configurationUpdate`; revisions 3–10 all
ship `{"componentVersion": "2.3.15"}` bare; Greengrass preserves the
device's last-applied config, so the `dda-model-status` shadow (added to
the auto-include by model-gpu-fallback-visibility, portal-deployed
2026-08-16 20:48Z) never reached the device — the device writes the shadow
locally (IPC `UpdateThingShadowResponse` v11→16) but cloud
`get-thing-shadow` returns `ResourceNotFoundException`. The
model-gpu-fallback-visibility cloud leg (portal Deployed-models panel) is
broken fleet-wide for every revised device, and the same class of bug will
bite ANY future named shadow.

The fix has two legs, both small and grounded in existing precedent in the
same files:

1. **Backend (the core)** — a shared ensure/merge helper in
   `edge-cv-portal/backend/functions/deployments.py`, applied at BOTH
   submission paths:
   - `create_deployment` (~L1190): the gate
     `if needs_nucleus and 'aws.greengrass.ShadowManager' not in
     components_map:` becomes `if needs_nucleus:` + the helper. Absent →
     today's fresh-add path, byte-identical semantics (full three-shadow
     synchronize merge, Nucleus-compatible resolved version, `auto_included`
     entry). Present → merge: bare entry gets the full portal merge
     injected; a stale merge gets the portal shadow names UNIONED into its
     `namedShadows` (never replaced), preserving caller extras and
     `direction`/`coreThing.classic` values; an explicit `componentVersion`
     is respected, a missing one is resolved. This mirrors the
     `elif needs_nucleus:` caller-supplied-Nucleus fallback (~L1284) that
     already exists for the store-limit merge — ShadowManager simply never
     got its equivalent.
   - `create_workflow_deployment` (~L3570): the copied previous-revision
     `components_map` gets the same ensure step applied to its
     ShadowManager entry, so workflow revisions stop propagating bare/stale
     entries indefinitely.
2. **Frontend** — `edge-cv-portal/frontend/src/pages/CreateDeployment.tsx`
   `preloadExistingComponents` (~L905) adds `aws.greengrass.ShadowManager`
   to the `autoManaged` skip set (currently only Nucleus + LogManager), so
   the UI revise flow stops resubmitting the entry bare in the first place
   and the backend fresh-add path manages it — consistent with
   `componentsToBeRemoved` (~L480), which already treats ShadowManager as
   portal-managed by excluding it from removal warnings.

The frontend leg fixes the common path at the source; the backend merge leg
is the durable guarantee that covers API callers, thing-group flows, older
cached frontends, and the workflow path — no submitted portal deployment
can carry a ShadowManager entry missing portal shadow names again.

## Glossary

- **Bug_Condition (C)**: a portal deployment submission whose component set
  already contains `aws.greengrass.ShadowManager` with a bare entry or a
  stale synchronize merge missing portal shadow names (see Bug Details).
- **Property (P)**: every submitted portal deployment that carries
  ShadowManager carries a synchronize merge whose `namedShadows` is a
  superset of the portal shadow names.
- **Preservation**: fresh-deploy auto-include semantics, caller extras and
  synchronize field values, explicit versions, all non-ShadowManager
  entries, and workflow-revision carry-over — all unchanged.
- **Portal shadow names**: `CAMERA_REGISTRY_SHADOW_NAME`
  (`dda-camera-registry`), `CAMERA_BINDINGS_SHADOW_NAME`
  (`dda-camera-bindings`), `MODEL_STATUS_SHADOW_NAME`
  (`dda-model-status`) — the module constants in `deployments.py`
  (L109-L118).
- **Bare entry**: a components-map entry with no `configurationUpdate`
  (e.g. `{"componentVersion": "2.3.15"}`) — Greengrass then preserves the
  device's last-applied configuration for that component.
- **Stale merge**: a `configurationUpdate.merge` whose
  `synchronize.coreThing.namedShadows` list is missing at least one portal
  shadow name.
- **`create_deployment`**: the generic deployment endpoint in
  `deployments.py`; builds `components_map` from the caller's list, runs
  the auto-include chain (LogManager, InferenceUploader, ShadowManager,
  Nucleus), and submits.
- **`create_workflow_deployment`**: the workflow deploy endpoint; on a
  revision it copies the previous deployment's components verbatim via
  `get_deployment` and (re)places only the workflow component entry.
- **`get_target_deployment`** (~L854): the UI prefill API; returns
  `component_name` + `component_version` ONLY per component —
  `configurationUpdate` is structurally dropped, which is why the UI
  revise flow resubmits ShadowManager bare.
- **`preloadExistingComponents`** (CreateDeployment.tsx ~L905): maps the
  prefill API's components into the UI selection, skipping the
  `autoManaged` set `{'aws.greengrass.Nucleus', 'aws.greengrass.LogManager'}`.
- **`resolve_shadow_manager_version`** (deployments.py ~L476): newest
  public ShadowManager version compatible with the device's running
  Nucleus, falling back to the pinned `SHADOW_MANAGER_VERSION` (2.3.15);
  returns the fallback immediately when `running_nucleus` is None.
- **`configurationUpdate.merge`**: a JSON **string** (the Greengrass API
  contract; every existing call site does `json.dumps(config)`); the merge
  helper must `json.loads` → mutate → `json.dumps`.

## Bug Details

### Bug Condition

The bug manifests when a portal deployment submission's component set
already contains `aws.greengrass.ShadowManager` — the UI revise flow always
resubmits it (bare, config dropped by the prefill API), and the workflow
revision path always copies it forward verbatim. The auto-include gate
`'aws.greengrass.ShadowManager' not in components_map` then skips entirely,
and the bare/stale entry ships. Greengrass preserves the device's
last-applied config for a bare entry, so shadow names added to the portal
list after the device's last CONFIGURED revision never sync.

**Formal Specification:**

```
FUNCTION isBugCondition(X)
  INPUT: X of type DeploymentSubmission
  OUTPUT: boolean

  RETURN 'aws.greengrass.ShadowManager' IN X.components_map
         AND NOT (portalShadowNames ⊆
                  namedShadows(X.components_map['aws.greengrass.ShadowManager']))
  // portalShadowNames = {dda-camera-registry, dda-camera-bindings,
  //                      dda-model-status}
  // namedShadows(entry) = entry.configurationUpdate.merge (JSON string,
  //                       parsed) → synchronize.coreThing.namedShadows,
  //                       ∅ when the entry is bare
END FUNCTION
```

### Examples

- **UI revise (the thor1 incident)**: revise the jetson-thor1 deployment in
  the portal → prefill returns ShadowManager as `2.3.15` name+version only
  → submitted `components_map` carries `{"componentVersion": "2.3.15"}` →
  the gate skips → revision ships bare. Expected: the submitted entry
  carries the full three-shadow synchronize merge. Actual: revisions 3–10
  all bare; device stuck on the rev-2 two-shadow config; `dda-model-status`
  cloud shadow `ResourceNotFoundException`.
- **Workflow revision**: `create_workflow_deployment` on a target whose
  previous revision has ShadowManager bare → copies it verbatim → ships
  bare forever. Expected: the copied entry gets the ensure/merge step.
- **Stale merge**: a target whose last configured revision predates
  model-gpu-fallback-visibility carries a 2-shadow merge; a revision
  resubmitting that merge keeps `dda-model-status` missing. Expected: the
  portal names are unioned in, the 2 existing names and field values kept.
- **Edge case (fresh deployment)**: component set WITHOUT ShadowManager —
  the auto-include fires today and must keep firing byte-identically
  (covered by `test_deployment_shadow_manager.py` and
  `test_model_status_shadow_sync.py`).
- **Edge case (caller extras)**: an API caller submits a merge with
  `namedShadows: ["custom-shadow"]` — the fixed code must produce
  `["custom-shadow", <the three portal names>]`, never drop the extra.

## Expected Behavior

### Preservation Requirements

**Unchanged Behaviors:**

- **Fresh-deploy auto-include (3.1)**: component set without ShadowManager
  → full three-shadow merge, Nucleus-compatible resolved version,
  `auto_included` entry + reason string — exactly as pinned by
  `test_deployment_shadow_manager.py::test_local_server_deployment_auto_includes_shadow_manager`
  and `test_model_status_shadow_sync.py`.
- **Caller extras (3.2)**: extra shadow names beyond the portal set survive
  the merge (union semantics — never replace or drop).
- **Synchronize field values (3.3)**: caller-supplied `direction`,
  `coreThing.classic`, and any other keys in the merge document survive
  byte-for-byte; portal defaults apply only to ABSENT keys.
- **Other components (3.4)**: entries other than ShadowManager are
  submitted untouched — versions, configurationUpdates, and the
  Nucleus/LogManager/InferenceUploader auto-includes all unchanged.
- **Explicit version (3.5)**: a submitted `componentVersion` is used as-is;
  resolution runs only when the entry lacks one.
- **Workflow carry-over (3.6)**: `create_workflow_deployment` still carries
  all other existing components verbatim and (re)places the workflow entry
  at the newly resolved registered version — the ensure step touches ONLY
  the ShadowManager entry, and only when one is present in the copied map.

**Scope:**

All submissions where the ShadowManager entry already satisfies
`portalShadowNames ⊆ namedShadows` are byte-identical no-ops (the helper
must not even re-serialize a compliant merge string), and all submissions
without ShadowManager follow today's fresh-add path verbatim. This
includes: deployments with no DDA/LocalServer component (`needs_nucleus`
false — no ShadowManager logic at all, unchanged), fresh workflow
deployments (no previous revision → no ShadowManager in the map → no-op),
and the Nucleus/LogManager skip sets in the frontend preload.

## Hypothesized Root Cause

The root cause is verified, not hypothesized — confirmed at every layer by
direct code and deployment-history evidence:

1. **The auto-include gate is presence-gated** (`deployments.py` ~L1190):
   `if needs_nucleus and 'aws.greengrass.ShadowManager' not in
   components_map:` — written for the original camera-registry-sync bugfix
   under the assumption "caller supplied it → caller configured it". The
   asymmetry with Nucleus is decisive: Nucleus has an `elif needs_nucleus:`
   fallback (~L1284) injecting the store-limit merge into a caller-supplied
   entry; ShadowManager has no such branch, and
   `test_deployment_shadow_manager.py::test_caller_supplied_shadow_manager_is_not_overridden`
   pins the defective skip as intended behavior.
2. **The prefill API structurally strips config** (`get_target_deployment`
   ~L854/L923): it returns `component_name` + `component_version` only, so
   the UI CANNOT resubmit the existing merge even in principle.
3. **The frontend preload forgot ShadowManager** (CreateDeployment.tsx
   ~L905): `autoManaged = {'aws.greengrass.Nucleus',
   'aws.greengrass.LogManager'}` — ShadowManager is resubmitted bare, while
   `componentsToBeRemoved` (~L480) already treats it as portal-managed.
4. **The workflow path has zero ShadowManager logic**
   (`create_workflow_deployment` ~L3570): `components_map =
   dict(detail.get('components', {}))` copies the previous revision
   verbatim; only the workflow entry is touched.
5. **Greengrass semantics complete the trap**: a bare component entry in a
   revision preserves the device's last-applied configuration — so one
   configured revision followed by any number of bare revisions freezes the
   device's shadow list forever (thor1: frozen at rev-2's two shadows
   through revisions 3–10).

## Design Decisions

### Decision 1: One shared helper, `ensure_shadow_manager_sync`, in deployments.py

A single module-level helper owns ALL portal ShadowManager synchronize
semantics (today they live inline in the auto-include block):

```
FUNCTION ensure_shadow_manager_sync(components_map, resolve_version)
  INPUT:  components_map — the dict about to be submitted (mutated in place)
          resolve_version — ZERO-ARG callable returning a pinned version
                            string; called ONLY when needed (lazily)
  OUTPUT: 'added' | 'merged' | 'unchanged'

  IF 'aws.greengrass.ShadowManager' NOT IN components_map:
      components_map[...] = full portal entry            // fresh-add path,
      //   componentVersion = resolve_version(),         // byte-identical
      //   configurationUpdate.merge = json.dumps(       // to today's
      //       PORTAL_SHADOW_SYNC_CONFIG)                // auto-include
      RETURN 'added'

  entry ← components_map['aws.greengrass.ShadowManager']
  IF 'componentVersion' NOT IN entry (or empty):
      entry['componentVersion'] ← resolve_version()      // 3.5: explicit
                                                         // version respected
  merged_names ← union-merge the synchronize config      // Decision 3
  IF nothing changed:
      RETURN 'unchanged'                                 // merge STRING left
                                                         // byte-identical
  RETURN 'merged'
END FUNCTION
```

Call sites:

- `create_deployment`: the gate collapses to `if needs_nucleus:` + helper,
  with `resolve_version=lambda: resolve_shadow_manager_version(
  greengrass_client, region, running_nucleus)`. `'added'` → the existing
  `auto_included` append + info log, verbatim (3.1). `'merged'` → info log
  only (Decision 4).
- `create_workflow_deployment`: after the previous revision's components
  are copied, `if 'aws.greengrass.ShadowManager' in components_map:` call
  the helper with `resolve_version=lambda: resolve_shadow_manager_version(
  greengrass_client, region, None)`. Presence-gated on purpose: the
  workflow path has no `needs_nucleus` concept and must not START
  auto-including ShadowManager on fresh workflow deployments (out of the
  requirements' scope; 3.6 keeps carry-over verbatim). Copied entries
  always carry `componentVersion` (`get_deployment` returns it), so the
  resolver effectively never fires there; if it ever does,
  `resolve_public_component_version` returns the pinned
  `SHADOW_MANAGER_VERSION` immediately for a None running-Nucleus — no new
  Nucleus lookup is added to the workflow path (minimal fix).

### Decision 2: Version resolution reuses `resolve_shadow_manager_version`, lazily

No new resolution logic. An explicit `componentVersion` on the submitted
entry is respected verbatim (3.5 — matches today's caller-supplied
behavior and the Nucleus precedent). Only a version-less entry triggers
`resolve_version()`, which is the existing Nucleus-compatible resolver in
the `create_deployment` path (running Nucleus already resolved up front
there) and the static-pin fallback in the workflow path. The resolver is a
zero-arg closure so the helper stays free of client/region plumbing and
the tests can stub it trivially.

### Decision 3: JSON merge semantics — parse, union, setdefault; byte-identical no-op

`configurationUpdate.merge` is a JSON **string**. The merge step:

1. `configurationUpdate` absent, or `merge` absent/empty → inject
   `{'merge': json.dumps(PORTAL_SHADOW_SYNC_CONFIG)}` (the bare case, 2.1
   — the full portal config: direction `betweenDeviceAndCloud`,
   `coreThing.classic = true`, the three portal shadow names).
2. `merge` present → `json.loads`. On `JSONDecodeError` (or a non-dict
   document): treat as bare — log a warning naming the target and replace
   with the full portal config. Nothing recoverable exists to preserve,
   and shipping a corrupt merge helps nobody.
3. Parsed document → navigate with `setdefault`:
   `synchronize` → `coreThing` (a non-dict value at either level is
   corrupt: log + replace that subtree with the portal defaults, siblings
   kept); `setdefault('direction', 'betweenDeviceAndCloud')` and
   `setdefault('classic', True)` — portal defaults fill ABSENT keys only,
   present values survive byte-for-byte (3.3); `namedShadows`: keep the
   existing list object and order, append each portal name not already in
   it, in portal-constant order (union — never replace, 2.2/3.2); a
   non-list `namedShadows` is corrupt → log + replace with the portal
   names. All unknown keys anywhere in the document survive untouched
   (`json.loads`/`json.dumps` round-trip of the same dict).
4. If step 3 changed nothing (all three names present, direction/classic
   present), the original merge STRING is left in place — not re-serialized
   — so a compliant submission is a byte-identical no-op (Preservation
   Scope).

### Decision 4: Merge-into-existing is logged, not reported in `auto_included`

The Nucleus `elif` precedent injects the store-limit merge into a
caller-supplied entry silently (no `auto_included` append). The
ShadowManager merge path follows it: `auto_included` keeps meaning "the
portal ADDED this component"; a merge into an existing entry emits one
`logger.info` line naming the entry state (bare/stale) and the resulting
shadow list. The fresh-add path's `auto_included` entry is unchanged.

### Decision 5: Frontend fix = one name in the `autoManaged` skip set

`preloadExistingComponents` adds `'aws.greengrass.ShadowManager'` to
`autoManaged`. The UI revise flow then omits it from the selection, the
backend sees it ABSENT, and the fresh-add path (3.1 semantics, freshly
resolved version, full merge, `auto_included` reporting) manages it — the
same lifecycle Nucleus and LogManager already have, and consistent with
`componentsToBeRemoved` (~L480) which already excludes
`aws.greengrass.ShadowManager` from removal warnings (no
`componentsToBeRemoved` change needed — the startsWith exclusion already
covers it). The backend merge leg (Decisions 1–4) remains the durable
guarantee for API callers, thing-group flows, and older cached frontends.

### Decision 6: ONE conscious pinned-test repoint

`test_deployment_shadow_manager.py::test_caller_supplied_shadow_manager_is_not_overridden`
pins the exact defect (requirement 1.1): a caller-supplied ShadowManager
entry is submitted untouched (`== {"componentVersion": "2.3.5"}`) with no
config. That assertion is repointed — never weakened or deleted — to the
2.1/3.5 contract: the caller's `2.3.5` version survives verbatim, the
submitted entry now carries the full portal synchronize merge, and no
`auto_included` entry is reported (Decision 4). The old assertions are
recorded verbatim in the preservation task BEFORE the fix so the repoint
diff is auditable. Every other test in that file (fresh-add, fake API
guard, no-LocalServer skip) and all of `test_model_status_shadow_sync.py`
must keep passing unmodified.

### Decision 7: Honesty guard — device sync truth is hardware-tier only

Every test in this spec proves properties of the SUBMITTED deployment
document (the `create_deployment_calls` capture in the moto/fake harness)
or of the UI's submission payload. None of them prove that a real device's
ShadowManager actually re-syncs `dda-model-status` to IoT Core — that
depends on real Greengrass config-merge semantics on the device and real
shadow sync. The real claim — thor1 revision 11 carries the merge, the
cloud `dda-model-status` shadow materializes, the portal Deployed-models
panel renders (closing model-gpu-fallback-visibility task 11's
shadow/portal leg) — is assigned exclusively to the USER ACTION
verification task. Do not write a test that pretends to exercise a real
device or the real account.

## Correctness Properties

Property 1: Bug Condition - Revised Deployments Carry the Full Portal Shadow Sync

_For any_ portal deployment submission where the bug condition holds
(`isBugCondition` returns true — the component set contains
`aws.greengrass.ShadowManager` bare or with a stale merge), the fixed
`create_deployment` and `create_workflow_deployment` SHALL submit a
ShadowManager entry whose `configurationUpdate.merge` (parsed) contains
`synchronize.coreThing.namedShadows` ⊇ {`dda-camera-registry`,
`dda-camera-bindings`, `dda-model-status`}, with a concrete
`componentVersion`; and the fixed `preloadExistingComponents` SHALL omit
`aws.greengrass.ShadowManager` from the preloaded selection so the UI
revise flow submits it absent.

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5**

Property 2: Preservation - Everything Outside the Bug Condition Is Unchanged

_For any_ input where the bug condition does NOT hold (`isBugCondition`
returns false — ShadowManager absent, or already carrying all portal
names), the fixed code SHALL produce the same result as the original code:
fresh deployments auto-include ShadowManager byte-identically to today,
already-compliant merges pass through with the merge string untouched,
caller extra shadow names and `direction`/`classic` values survive,
explicit `componentVersion`s are used as-is, all non-ShadowManager entries
are untouched, and workflow revisions carry over all other components
verbatim.

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**

Property 3: Fix Checking - Union-Not-Replace Merge Semantics (helper level)

_For any_ generated submitted-entry shape — bare (no configurationUpdate),
stale merge (any strict subset of the portal names, with or without extra
caller names, with or without explicit direction/classic values, with
unknown extra keys), corrupt merge (non-JSON string, non-dict nodes,
non-list namedShadows), and compliant merge — `ensure_shadow_manager_sync`
SHALL produce an entry whose parsed merge contains all portal names AND
every parseable caller-supplied name AND every caller-supplied field value
(union, setdefault-only defaults), respect an explicit `componentVersion`
without calling the resolver, resolve a missing one exactly once, and
return 'unchanged' with a byte-identical merge string for compliant input.

**Validates: Requirements 2.1, 2.2, 3.2, 3.3, 3.5**

Property 4: Fix Checking - Both Call Sites End-to-End

_For any_ revision-shaped submission through the real endpoints (the
`ShadowManagerEnv` harness for `create_deployment`; the workflow-deploy
harness for `create_workflow_deployment` with a previous revision carrying
a bare or stale ShadowManager entry), the SUBMITTED deployment (the fake's
`create_deployment_calls` capture) SHALL satisfy `portalShadowNames ⊆
namedShadows`, and the workflow path SHALL still carry every other copied
component verbatim and (re)place the workflow entry at the resolved
registered version.

**Validates: Requirements 2.1, 2.2, 2.3, 2.5, 3.6**

Property 5: Fix Checking - Frontend Preload Skip Set

_For any_ existing deployment returned by the prefill API that includes
`aws.greengrass.ShadowManager`, the fixed `preloadExistingComponents`
SHALL exclude it from the preloaded selection (as it already does Nucleus
and LogManager) while preloading all other components unchanged.

**Validates: Requirements 2.4, 3.4**

## Fix Implementation

### Changes Required

**File 1**: `edge-cv-portal/backend/functions/deployments.py`

**Functions**: NEW `ensure_shadow_manager_sync` (+ a module-level
`PORTAL_SHADOW_SYNC_CONFIG`-building helper or constant reusing the three
shadow-name constants), `create_deployment`, `create_workflow_deployment`

**Specific Changes**:
1. **Extract the portal sync config**: the `shadow_manager_config` dict
   currently built inline in the auto-include block becomes the single
   shared source (function or constant) so the fresh-add and merge paths
   cannot drift.
2. **Add `ensure_shadow_manager_sync(components_map, resolve_version)`**
   per Decisions 1–3 (returns 'added' | 'merged' | 'unchanged').
3. **`create_deployment` (~L1190)**: replace the presence-gated block with
   `if needs_nucleus:` + the helper; keep the existing `auto_included`
   append + log for 'added' verbatim; add one info log for 'merged'
   (Decision 4).
4. **`create_workflow_deployment` (~L3570)**: after `components_map` is
   copied from the previous revision (and independent of the workflow
   entry placement), presence-gated helper call with the static-fallback
   resolver (Decision 1).

**File 2**: `edge-cv-portal/frontend/src/pages/CreateDeployment.tsx`

**Function**: `preloadExistingComponents` (~L905)

**Specific Changes**:
5. **Add `'aws.greengrass.ShadowManager'` to the `autoManaged` set**
   (Decision 5). One-line change; `componentsToBeRemoved` needs no change.

**File 3** (conscious test repoint, Decision 6):
`edge-cv-portal/backend/tests/test_deployment_shadow_manager.py`

**Specific Changes**:
6. **Repoint `test_caller_supplied_shadow_manager_is_not_overridden`** to
   the 2.1/3.5 contract: caller's `componentVersion` `2.3.5` submitted
   verbatim; the entry now carries the portal synchronize merge; no
   `auto_included` ShadowManager entry. Rename/redocument so the test name
   states the new contract. Every other test in the file unchanged.

**Explicitly NOT changed**: `get_target_deployment` (the prefill API keeps
returning name+version only — with the skip set the UI no longer needs the
config), `resolve_shadow_manager_version` /
`resolve_public_component_version`, the Nucleus/LogManager/
InferenceUploader auto-includes, `apply_subscribe_access_control`,
`componentsToBeRemoved`, `devices.py`, any `src/` device-side file, any
recipe, any Dockerfile. **No preservation-tracked file is touched → no
security-baseline rebaselines** (the gate task verifies the claim). No
component build is required — this is a portal-only fix shipped by a
portal deploy.

## Testing Strategy

### Validation Approach

Two-phase per the bugfix methodology: first surface the counterexamples on
the UNFIXED code (exploration), baseline what must survive (preservation,
observation-first), then implement and verify the flip plus the fix-check
property suites. Portal backend suites run from `edge-cv-portal/backend`
WITH conftest (moto `aws_stack` fixture; Hypothesis profiles are
conftest-registered — no hardcoded `max_examples`) in the portal venv:
`source /home/ubuntu/.venvs/dda-portal-tests/bin/activate` then
`python3 -m pytest tests/<file> -q -p no:cacheprovider`. Frontend is a
vitest single run: `npx vitest run <file>` from `edge-cv-portal/frontend`.

### Exploratory Bug Condition Checking

**Goal**: Surface counterexamples that demonstrate the bug BEFORE
implementing the fix; confirm the root-cause analysis (the presence gate,
the workflow copy, the preload set).

**Test Plan**: NEW
`edge-cv-portal/backend/tests/test_shadowmanager_sync_revision_exploration.py`
(reusing the `ShadowManagerEnv` harness and the workflow-deploy fixtures)
plus a frontend exploration leg. Run on the UNFIXED code and observe
failures.

**Test Cases**:
1. **Revise-shape bare entry** (defects 1.1/1.2): `create_deployment` with
   LocalServer + a bare ShadowManager entry (the exact thor1 revision
   shape, version `2.3.15`) → assert the submitted entry's parsed merge
   lists all three portal names (will fail on unfixed code — the submitted
   entry is `{"componentVersion": "2.3.15"}`, no configurationUpdate)
2. **Stale two-shadow merge** (defect 1.4 origin): caller entry carrying
   the OLD rev-2 merge (`dda-camera-registry`, `dda-camera-bindings`) →
   assert `dda-model-status` is unioned in (will fail on unfixed code —
   submitted merge unchanged, two names)
3. **Workflow revision copies bare entry forward** (defect 1.3): seed a
   previous revision whose components include bare ShadowManager; deploy a
   workflow revision → assert the submitted ShadowManager entry carries
   the full merge (will fail on unfixed code — copied verbatim)
4. **Frontend preload resubmits ShadowManager** (defect 1.2): vitest leg —
   prefill mock returning ShadowManager among the existing components →
   assert the preloaded selection omits it (will fail on unfixed code —
   `autoManaged` lacks the name)

**Expected Counterexamples**:
- The submitted `components_map['aws.greengrass.ShadowManager']` equal to
  the caller's bare/stale entry, byte-for-byte — the gate skipped
- Possible causes confirmed: the presence gate (~L1190), the verbatim
  workflow copy (~L3570), the two-name `autoManaged` set (~L905)

### Fix Checking

**Goal**: For all inputs where the bug condition holds, the fixed
functions produce the expected behavior.

**Pseudocode:**
```
FOR ALL X WHERE isBugCondition(X) DO
  submitted := create_deployment'(X)   // or create_workflow_deployment'(X)
  ASSERT portalShadowNames ⊆ namedShadows(submitted['aws.greengrass.ShadowManager'])
  ASSERT callerNames(X) ⊆ namedShadows(submitted[...])          // union
  ASSERT callerFieldValues(X) preserved (direction, classic, unknown keys)
  ASSERT explicit componentVersion(X) respected, else resolved
END FOR
```

### Preservation Checking

**Goal**: For all inputs where the bug condition does NOT hold, the fixed
functions produce the same result as the original functions.

**Pseudocode:**
```
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT create_deployment(X) = create_deployment'(X)
  ASSERT create_workflow_deployment(X) = create_workflow_deployment'(X)
  ASSERT preloadExistingComponents(X) = preloadExistingComponents'(X)
    EXCEPT the ShadowManager omission (2.4 — the intended change)
END FOR
```

**Testing Approach**: Property-based testing (Hypothesis backend-side,
fast-check where a frontend property fits) — preservation is a universal
claim over the input domain and PBT catches the edge shapes (empty lists,
unknown keys, version-less entries) manual cases miss.

**Test Plan**: Observe on UNFIXED code first, record, then encode:
1. **Fresh-deploy identity**: baseline `test_deployment_shadow_manager.py`
   (4 tests) and `test_model_status_shadow_sync.py` (2 tests) green with
   recorded counts; record VERBATIM the caller-supplied test's assertions
   (the Decision 6 repoint target)
2. **Compliant-merge no-op PBT**: _for any_ entry already carrying all
   portal names (plus generated extras/field values), the submitted merge
   string is byte-identical (skip-as-absent against the helper until it
   lands; end-to-end form runs on unfixed code and must keep passing)
3. **Non-ShadowManager identity**: the full submitted components_map minus
   the ShadowManager key deep-equals the unfixed capture for generated
   component sets (both endpoints)
4. **Workflow carry-over identity**: baseline the existing workflow-deploy
   suites (`test_workflow_deploy_subscribe_merge_*`,
   `test_workflow_deploy_component_version_*`,
   `test_workflow_packaging_deployment_integration.py`,
   `test_camera_binding_submission.py`) green with recorded counts — none
   of their fixtures carry ShadowManager, so they must pass UNMODIFIED
5. **Frontend preload identity**: preloaded selection for existing
   components WITHOUT ShadowManager unchanged (vitest; the
   `CreateDeployment.archFilter.test.tsx` revise fixture stays green
   unmodified)

### Unit Tests

- Helper: bare entry → full merge; version-less entry → resolver called
  once; explicit version → resolver never called; corrupt merge string →
  full portal config + warning; non-list `namedShadows` → replaced;
  compliant → 'unchanged'
- `create_deployment`: 'added' keeps the exact `auto_included` entry;
  'merged' adds no `auto_included` entry (Decision 4)
- `create_workflow_deployment`: fresh (no previous revision) → no
  ShadowManager entry appears (presence gate)
- The Decision 6 repointed test (caller version respected + merge injected)

### Property-Based Tests

- **Property 3** (Hypothesis): generated entry shapes — bare / stale
  subsets / extras / explicit-vs-missing version / unknown keys / corrupt
  variants — through `ensure_shadow_manager_sync`; union-not-replace,
  setdefault-only, byte-identical no-op assertions
- **Property 4** (Hypothesis): generated revision-shaped submissions
  through BOTH real endpoints (fake harness); submitted-document superset
  assertion + workflow carry-over identity
- **Preservation PBTs** (Hypothesis): compliant no-op; non-ShadowManager
  map identity

### Integration Tests

- End-to-end thor1-shape replay: `create_deployment` revise with the exact
  revision-10 component list (bare ShadowManager 2.3.15) → submitted
  document carries the three-shadow merge; then a workflow revision over
  that result stays compliant (the two paths compose)
- Frontend component test (vitest, `CreateDeployment.archFilter.test.tsx`
  conventions — mocked `apiService` proxy, Cloudscape test-utils): revise
  flow with ShadowManager in the prefill → selection omits it, other
  components preloaded, submission payload carries no ShadowManager entry
- **USER ACTION tier (Decision 7)**: thor1 revision-11 — the real device
  sync claim lives ONLY here
