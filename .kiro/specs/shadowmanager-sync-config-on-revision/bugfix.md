# Bugfix Requirements Document

## Introduction

The portal's ShadowManager `synchronize` auto-include in
`edge-cv-portal/backend/functions/deployments.py` `create_deployment` is gated
on `if needs_nucleus and 'aws.greengrass.ShadowManager' not in components_map:`
— it only fires when `aws.greengrass.ShadowManager` is ABSENT from the
submitted component set. Two portal flows always submit ShadowManager, so the
auto-include is permanently disarmed for any target after its first revision:

1. **UI revise flow**: `edge-cv-portal/frontend/src/pages/CreateDeployment.tsx`
   `preloadExistingComponents` preloads the existing deployment's components,
   skipping only `autoManaged = {'aws.greengrass.Nucleus',
   'aws.greengrass.LogManager'}`. ShadowManager is NOT in the skip set, so it
   is resubmitted bare — the prefill API `get_target_deployment` returns
   `component_name` + `component_version` only, dropping `configurationUpdate`.
   The backend then ships `{"componentVersion": "2.3.15"}` with NO synchronize
   config.
2. **Workflow deployment path**: `create_workflow_deployment` copies the
   previous revision's components verbatim into `components_map` and has no
   ShadowManager logic at all.

Because Greengrass preserves the device's last-applied component configuration
when a revision ships the component bare, revised targets keep whatever
synchronize config their LAST configured revision carried — forever. Any shadow
name later added to the portal's auto-include list (e.g. `dda-model-status`
from the model-gpu-fallback-visibility spec) never reaches previously deployed
targets, and the same class of bug will bite ANY future named shadow.

Note the asymmetry with Nucleus: `create_deployment` has an
`elif needs_nucleus:` fallback (~L1284) that injects the Nucleus store-limit
config even when the caller supplied Nucleus explicitly; ShadowManager has no
such branch. Also, `componentsToBeRemoved` in CreateDeployment.tsx (~L480)
already treats ShadowManager as portal-managed (excluded from removal
warnings) — the frontend preload just forgot it.

### Incident Record (verified evidence, jetson-thor1)

- Deployment history verified via `get-deployment` on every revision of the
  jetson-thor1 target: **revision 2 (Aug 14)** was the last to carry a
  ShadowManager `configurationUpdate` merge — the OLD 2-shadow list
  (`dda-camera-registry`, `dda-camera-bindings`).
- **Revisions 3–10** all carry ShadowManager BARE
  (`{"componentVersion": "2.3.15"}` only); Greengrass preserves the rev-2
  device config.
- The `dda-model-status` shadow (added to the auto-include by the
  model-gpu-fallback-visibility spec, portal-deployed 2026-08-16 20:48Z) never
  reached the device. Verified live on thor1: the device writes the shadow
  locally (ShadowManager IPC `UpdateThingShadowResponse` v11→16), but cloud
  `get-thing-shadow` → `ResourceNotFoundException`; effective device config
  `namedShadows=[dda-camera-registry, dda-camera-bindings]`.
- Impact: the model-gpu-fallback-visibility spec's cloud leg (portal
  Deployed-models panel) is broken fleet-wide for every existing (revised)
  device.

### Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type DeploymentSubmission
  OUTPUT: boolean

  // A portal deployment (create_deployment with needs_nucleus, or
  // create_workflow_deployment revision) whose submitted/copied component
  // set already contains aws.greengrass.ShadowManager — with a bare entry
  // or a stale synchronize merge missing portal shadow names.
  RETURN 'aws.greengrass.ShadowManager' IN X.components_map
         AND NOT (portalShadowNames ⊆
                  namedShadows(X.components_map['aws.greengrass.ShadowManager']))
  // where portalShadowNames = {dda-camera-registry, dda-camera-bindings,
  //                            dda-model-status} (CAMERA_REGISTRY_SHADOW_NAME,
  //                            CAMERA_BINDINGS_SHADOW_NAME,
  //                            MODEL_STATUS_SHADOW_NAME)
  // and namedShadows(entry) = the synchronize.coreThing.namedShadows list in
  //                            the entry's configurationUpdate merge
  //                            (∅ when the entry is bare)
END FUNCTION
```

## Bug Analysis

### Current Behavior (Defect)

1.1 WHEN `create_deployment` receives a component set that includes
`aws.greengrass.ShadowManager` (the revise flow always does) THEN the system
skips the ShadowManager auto-include entirely and submits the entry as
provided — bare, with no `synchronize` configuration merge

1.2 WHEN a user revises an existing deployment in the UI THEN
`preloadExistingComponents` in CreateDeployment.tsx resubmits
`aws.greengrass.ShadowManager` from the existing deployment (it is missing
from the `autoManaged` skip set), stripped of its `configurationUpdate`
because the `get_target_deployment` prefill API returns name+version only

1.3 WHEN `create_workflow_deployment` revises a target THEN the system copies
the previous revision's components verbatim into the new deployment with no
ShadowManager synchronize ensure step, propagating a bare or stale
ShadowManager entry indefinitely

1.4 WHEN a revised deployment ships `aws.greengrass.ShadowManager` bare THEN
Greengrass preserves the device's last-applied synchronize config, so shadow
names added to the portal auto-include list after that device's last
configured revision (e.g. `dda-model-status`) never sync to IoT Core — the
device writes the shadow locally but cloud `get-thing-shadow` returns
`ResourceNotFoundException`

### Expected Behavior (Correct)

2.1 WHEN `create_deployment` receives a component set that includes
`aws.greengrass.ShadowManager` with a bare entry (no `configurationUpdate`)
THEN the system SHALL inject the full portal `synchronize` configuration merge
(direction `betweenDeviceAndCloud`, `coreThing.classic = true`, `namedShadows`
containing `CAMERA_REGISTRY_SHADOW_NAME`, `CAMERA_BINDINGS_SHADOW_NAME`, and
`MODEL_STATUS_SHADOW_NAME`) into that entry, following the same
caller-supplied-entry fallback pattern already used for Nucleus

2.2 WHEN `create_deployment` receives a component set that includes
`aws.greengrass.ShadowManager` with an existing `synchronize` merge whose
`namedShadows` list is missing any portal shadow name THEN the system SHALL
union the portal shadow names into that list (never replace it)

2.3 WHEN `create_workflow_deployment` copies a previous revision's components
that include `aws.greengrass.ShadowManager` THEN the system SHALL apply the
same ensure/merge step to the copied entry so the submitted deployment carries
a `synchronize` merge whose `namedShadows` contains all portal shadow names

2.4 WHEN `preloadExistingComponents` in CreateDeployment.tsx preloads an
existing deployment's components THEN the frontend SHALL skip
`aws.greengrass.ShadowManager` (add it to the `autoManaged` skip set) so the
backend auto-include manages it, consistent with `componentsToBeRemoved`
already treating it as portal-managed

2.5 WHEN any portal deployment revision is created for a target whose previous
revision carried a bare or stale ShadowManager entry THEN the resulting
deployment SHALL carry a ShadowManager `synchronize` merge listing all three
portal shadow names, so newly added shadows (e.g. `dda-model-status`) reach
existing devices on their next revision

### Unchanged Behavior (Regression Prevention)

3.1 WHEN `create_deployment` receives a component set that does NOT include
`aws.greengrass.ShadowManager` (a fresh deployment) THEN the system SHALL
CONTINUE TO auto-include ShadowManager with the full three-shadow
`synchronize` merge and the Nucleus-compatible resolved version, exactly as
covered by the existing tests in
`edge-cv-portal/backend/tests/test_model_status_shadow_sync.py`

3.2 WHEN a submitted ShadowManager entry's `namedShadows` list contains
caller-supplied EXTRA shadow names beyond the portal set THEN the system SHALL
CONTINUE TO preserve those extra names (union semantics — never replace or
drop caller entries)

3.3 WHEN a submitted ShadowManager entry carries `synchronize` fields such as
`direction` and `coreThing.classic` THEN the system SHALL CONTINUE TO preserve
those field values when merging shadow names into the entry

3.4 WHEN a deployment contains component entries other than
`aws.greengrass.ShadowManager` THEN the system SHALL CONTINUE TO submit those
entries untouched (versions, configurationUpdates, and the Nucleus/LogManager
auto-include behavior all unchanged)

3.5 WHEN a submitted ShadowManager entry carries an explicit
`componentVersion` THEN the system SHALL CONTINUE TO use that version rather
than re-resolving it

3.6 WHEN `create_workflow_deployment` revises a target THEN the system SHALL
CONTINUE TO carry over all other existing components verbatim and (re)place
the workflow component entry at the newly resolved registered version
