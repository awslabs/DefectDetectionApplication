# Bugfix Requirements Document

## Introduction

In the Workflow_Builder, an `aravis_camera_source` node's camera picker offers the
registry-backed **Static Image Camera** on device `jetson-thor1`, but selecting it leaves the
node invalid. The panel reports `Current value: (not set) — linked to Static Image Camera on
jetson-thor1` and the node carries the violation `Required parameter 'camera_id' has no value`.
The picker therefore offers a selection it structurally cannot bind — a dead end.

The cause is a shape mismatch between the device's Static_Image_Camera inventory entry and the
Portal's Aravis id resolution. `cameraIdValue()` in
`edge-cv-portal/frontend/src/pages/workflows/cameraReference.ts` resolves the Aravis camera id
exclusively from `params.cameraId`, and `applyAravisCameraSelection()` writes `camera_id` only
when that resolution is non-null. The device, however, reports the static camera with an empty
`params` block and the id inside the capabilities block. Verified in the live `dda-camera-registry`
named shadow for `jetson-thor1` (`reported.cameras["static-image-camera"]`): `type: "StaticImage"`,
`origin: "edge-discovered"`, `params: {}`, `capabilities.staticImage.id: "static-image-camera"`.
That shape is produced device-side by `_static_image_entry()` / `_static_image_absent_entry()` in
`src/backend/camera_sync/inventory.py`, which build `CameraSourceState(..., params={},
capabilities={"staticImage": _static_image_identity()})`. Consequently `cameraIdValue()` returns
null for every StaticImage entry and `camera_id` is never populated.

The rest of the static-camera path is wired correctly and must keep working: the deploy-time
validator already accepts the type (compatible set `{Camera, AravisDiscovered, StaticImage}` in
`edge-cv-portal/backend/functions/deployments.py`), `isAravisCompatibleCamera()` correctly offers
the type, and the device serves the static camera through the same aravis frame-feed path bus
cameras use, under the fixed enumeration id `static-image-camera`.

A second, secondary defect turns the recovery path into its own dead end. The node panel's
"Pin a static test image…" shortcut opens `/devices/{id}?usecase_id=…&tab=cameras`, which lands
on the Cameras tab at the top of the page. The `StaticImagePanel` renders *below* the device
cameras table; `jetson-thor1` reports 9 cameras, so the pin controls are off-screen on arrival.
The visually prominent action at the top is "Create camera source", whose type options
deliberately exclude StaticImage (the static camera is `origin: edge-discovered`, and the Portal
rejects manual creation of discovery-managed sources). The user follows the shortcut and finds no
way to attach an image.

A third defect, found by live on-device investigation after the first two were written up, explains
why the picker looked confusing in the first place: the Static_Image_Camera is registered **twice**
in the Camera_Registry. `GET /devices/jetson-thor1/cameras` returns 9 cameras, two of which are the
same virtual camera under different ids and types — `arv-6c84191b7fe6` (`type: "AravisDiscovered"`,
name `AWS-DDA Static Image Camera`, `params.cameraId: "static-image-camera"`) and
`static-image-camera` (`type: "StaticImage"`, `params: {}`, identity under
`capabilities.staticImage`). Both are `absent` while nothing is pinned. For contrast the healthy
Aravis Fake camera on the same device is a single entry (`arv-c9dd20f60ee1`, `Aravis Fake`,
present, `params.cameraId: "Fake_1"`).

The duplication is two features each appending their own entry with no exclusion between them.
`getCameras()` in `src/backend/edge_ml1_p_camera_management/aravis_functions.py` appends
`Camera(**STATIC_IMAGE_CAMERA_IDENTITY)` to the bus enumeration while a Pinned_Image exists (base
spec static-image-camera-source, Requirements 1.2, 2.1-2.3, 2.5-2.7). That synthetic bus entry
flows into `src/backend/camera_discovery/aravis.py` `enumerate_aravis()` / `_map_camera()`, which
derives `stable_id = aravis_stable_id("AWS-DDA", "Static Image Camera", "STATIC-IMAGE-0")` and
yields a `DiscoveredAravisCamera` the inventory reports as an `AravisDiscovered` entry with
`params.cameraId` populated. That derivation was recomputed from the shipped constants and equals
`arv-6c84191b7fe6` character-for-character — the id in the live payload. Meanwhile
`build_inventory()` in `src/backend/camera_sync/inventory.py` appends its own dedicated entry
(`if static_image_pinned: entries.append(_static_image_entry(...))`, line ~279) under
`STATIC_IMAGE_CAMERA_ID` (cloud-static-camera-provisioning Requirement 6.1). Verified: nothing in
`build_inventory` excludes the aravis-enumerated static camera from the discovery entries before
appending its own — the only `STATIC_IMAGE_CAMERA_ID` uses in that module are the import, the name
constant, and the two entry builders.

The consequence is user-visible and interacts with Defect 1: the Aravis picker offers two entries
for one camera, and the user picked the `StaticImage`-typed one — the one that hits Defect 1's
unbindable `camera_id`. The `AravisDiscovered` duplicate would have bound. Two entries also mean
two independent absence lifecycles for one camera.

This is a three-part bugfix, split across two independently shippable deliverables:
1. (Portal, frontend-only) Resolve the Aravis camera id for StaticImage entries from the
   capabilities block when `params.cameraId` is absent, so the offered selection binds.
2. (Portal, frontend-only) Make the static-image pin panel discoverable when the Cameras tab is
   reached through the node panel's shortcut.
3. (Device, LocalServer component) De-duplicate the Static_Image_Camera in the reported inventory,
   so the registry holds exactly one entry for it in every pin state.

Parts 1 and 2 deploy in minutes through `./deploy-frontend.sh`. Part 3 needs a Greengrass component
build (~100 minutes, user-driven) plus a deployment revision to reach devices, so the two tracks are
deliberately kept independent: neither blocks the other, and part 3 only depends on parts 1 and 2 in
the sense that after both land, whichever of the two registry entries a user picks binds correctly.

Scope is deliberately narrow in each part. The device-side inventory *shape* (`params={}` plus the
capabilities identity) is the shipped, hardware-verified contract that the named shadow, the
absence-reporting behavior, and the Portal's discovery-managed mutation rejection all depend on;
this fix changes which entries are reported, never that entry's shape.
`isAravisCompatibleCamera()` is correct as it stands. `getCameras()` keeps appending the static
camera: the device's own `/cameras` route, `rescan_cameras()`, the `getCamera()` short-circuit,
Image_Source creation, the camera-manager grab path, and local aravis frame feeds all depend on it
enumerating like a GenICam camera (base spec Requirements 2.1-2.3, 3.x, 4.1). The cloud REPORT is
the only place that should de-duplicate. The `absent` badge on `jetson-thor1` is also correct, not a
defect: the live status route reports an applied `remove` request with `deviceReported.present:
false`, so nothing is pinned. The Aravis Fake camera is likewise not a defect — it was live-verified
healthy end to end, and the original report of it "missing" is explained by it being bus-discovered
(so absent from the "Create camera source" type list) rather than broken.

## Bug Analysis

### Current Behavior (Defect)

Observed on the deployed portal `d23v4ltibogb5x.cloudfront.net` (account 164152369890, us-east-1,
rest-api `yqvyoowugk`) and confirmed against the shipped source.

1.1 WHEN a Camera_Source entry of type `StaticImage` is resolved for the Aravis picker — the shipped device shape, with an empty `params` block and the id under `capabilities.staticImage.id` — THEN the system resolves no camera id at all, because `cameraIdValue()` reads only `params.cameraId` and returns null

1.2 WHEN the user selects the Static Image Camera in an `aravis_camera_source` node's camera picker THEN the system records the advisory binding hint but leaves `camera_id` unpopulated, because `applyAravisCameraSelection()` writes `camera_id` only while `cameraIdValue()` is non-null, so the panel reads `Current value: (not set) — linked to Static Image Camera on jetson-thor1`

1.3 WHEN the `aravis_camera_source` node is validated after that selection THEN the system reports the violation `Required parameter 'camera_id' has no value` and the node stays invalid, so the picker offered a source that cannot be bound

1.4 WHEN the Aravis picker renders the Static Image Camera option THEN the system shows the option without a camera-id description, because the option description is fed by the same null-returning `cameraIdValue()`

1.5 WHEN the user clicks "Pin a static test image…" in the node configuration panel THEN the system opens the device Cameras tab at the top of the page with no scroll target, anchor, or highlight, and the "Static image camera" panel — rendered below a 9-row camera table on `jetson-thor1` — is off-screen on arrival

1.6 WHEN the user looks for a way to attach an image on that page THEN the system presents "Create camera source" as the prominent action, whose type options (Camera (V4L2), NVIDIA CSI, RTSP, Folder, ICam) exclude StaticImage by design, leaving no visible path to pin a test image

1.7 WHEN a Pinned_Image exists THEN the system enumerates the synthetic static camera on the Aravis bus (`getCameras()` appends `Camera(**STATIC_IMAGE_CAMERA_IDENTITY)`) and Camera_Discovery maps it like a physical GenICam camera, deriving the stable id `aravis_stable_id("AWS-DDA", "Static Image Camera", "STATIC-IMAGE-0")` = `arv-6c84191b7fe6` and tracking it in the discovery snapshot

1.8 WHEN `build_inventory()` merges that discovery snapshot with `static_image_pinned=True` THEN the system reports TWO Camera_Sources for the one Static_Image_Camera — an `AravisDiscovered` entry under `arv-6c84191b7fe6` carrying `params.cameraId: "static-image-camera"`, plus the dedicated `StaticImage` entry under `static-image-camera` — because the merge appends its own entry without excluding the aravis-enumerated one from the reported discovery entries

1.9 WHEN the Aravis picker lists the device's registry entries THEN the system offers both duplicates for one camera ("AWS-DDA Static Image Camera" and "Static Image Camera"), so the user who picks the `StaticImage`-typed one hits the unbindable `camera_id` of 1.2 and 1.3 while the `AravisDiscovered` duplicate would have bound

1.10 WHEN the image is unpinned THEN the system runs two independent absence lifecycles for the one camera — Camera_Discovery marks `arv-6c84191b7fe6` absent when it leaves the bus enumeration, while the dedicated entry is reported absent from `static_image_absent_since` — and (live on `jetson-thor1`) the registry holds both entries absent at the same time

1.11 WHEN a device build simply OMITS the duplicate from a full report THEN the system leaves the `arv-6c84191b7fe6` key alive in the shadow document, because AWS IoT shadow updates MERGE nested maps and every documents event therefore still carries the key; the Portal's missing-from-report deletion path (`_deletion_candidates` in `edge-cv-portal/backend/functions/camera_sync.py`) never sees it as missing, so the already-published duplicate would survive the fix indefinitely on any device that already reported it

### Expected Behavior (Correct)

2.1 WHEN a Camera_Source entry of type `StaticImage` carries a non-empty string id at `capabilities.staticImage.id` and no usable `params.cameraId` THEN the system SHALL resolve that capabilities id as the Aravis camera id, reading it from the capabilities block rather than from a hardcoded constant so a future identity change flows through

2.2 WHEN the user selects the Static Image Camera in an `aravis_camera_source` node's camera picker THEN the system SHALL populate the node's `camera_id` parameter with the resolved id (`static-image-camera` for the shipped enumeration identity) alongside the advisory binding hint, so the panel reports that value as the current value instead of `(not set)`

2.3 WHEN the `aravis_camera_source` node is validated after that selection THEN the system SHALL NOT report `Required parameter 'camera_id' has no value`; every source the Aravis picker offers SHALL yield a bindable, valid node

2.4 WHEN the Aravis picker renders the Static Image Camera option THEN the system SHALL describe the option by the resolved camera id, consistently with how bus cameras and configured Camera sources are described

2.5 WHEN the user clicks "Pin a static test image…" in the node configuration panel THEN the system SHALL land on the device Cameras tab with the "Static image camera" panel brought into view and visually flagged, so the pin controls are the obvious next action on arrival

2.6 WHEN the user reaches the Cameras tab through that shortcut and opens the "Create camera source" form THEN the system SHALL point at the static-image panel as the place to pin a test image, without offering StaticImage as a creatable type

2.7 WHEN `build_inventory()` merges a discovery result that carries the aravis-enumerated static camera (a discovered Aravis camera whose `camera_id` equals `STATIC_IMAGE_CAMERA_ID`) and that camera is not referenced by any configured Image_Source THEN the system SHALL exclude it from the reported discovery entries and report exactly ONE Camera_Source for the Static_Image_Camera — the dedicated entry under `STATIC_IMAGE_CAMERA_ID`, which additionally carries the pin metadata and the explicit-absence lifecycle the derived `arv-` entry cannot

2.8 WHEN the pin state is pinned, unpinned-after-having-been-reported, or never pinned THEN the system SHALL report exactly one entry for the Static_Image_Camera in the first state (present, with pin metadata), exactly one in the second (explicitly `absent` with a stable `absentSince`), and zero in the third — with no `arv-*` duplicate in any of the three states

2.9 WHEN a device that has ALREADY published the `arv-6c84191b7fe6` duplicate runs the fixed build THEN the system SHALL converge that published registration to gone rather than leaving it stale: it SHALL delete the reported shadow key explicitly once (a null-valued `cameras` key — the only way to remove a key from a merged nested map, mirroring the Portal's existing `_clear_static_camera_shadow_key` cleanup), so the Portal's missing-from-report path then removes the registry entry and the picker offers one option

2.10 WHEN the Portal reads the device's camera list after the fixed build's first report THEN the system SHALL show exactly one static-image camera row, the Aravis picker SHALL offer exactly one Static Image Camera option, and selecting it SHALL bind `camera_id` per 2.2

2.11 WHEN the retired duplicate's key is derived THEN the system SHALL derive it from the shipped enumeration identity through `aravis_stable_id()` rather than hardcoding `arv-6c84191b7fe6`, so a future change to the fixed identity flows through to both the exclusion and the retirement

### Unchanged Behavior (Regression Prevention)

3.1 WHEN a Camera_Source of type `Camera` or `AravisDiscovered` carries a non-empty string `params.cameraId` THEN the system SHALL CONTINUE TO resolve exactly that value as the camera id, with the capabilities lookup never overriding a usable `params.cameraId`

3.2 WHEN a Camera_Source carries neither a usable `params.cameraId` nor a non-empty `capabilities.staticImage.id` THEN the system SHALL CONTINUE TO resolve null, and applying the selection SHALL CONTINUE TO retain any pre-existing `camera_id` parameter value (or leave the parameter absent when there was none)

3.3 WHEN a selection is applied to an `aravis_camera_source` node THEN the system SHALL CONTINUE TO copy `gain` and `exposure` exactly when the source's params carry them as numbers, leave every other parameter untouched, avoid mutating its inputs, and produce the binding hint carrying the source id, display name, and reference device id

3.4 WHEN the Aravis picker filters the device's registry entries THEN the system SHALL CONTINUE TO offer exactly the compatible set — type `AravisDiscovered`, type `StaticImage`, and type `Camera` carrying a non-empty string `params.cameraId` — with `isAravisCompatibleCamera` unchanged and the V4L2/ICAM filter `isV4l2CompatibleCamera` unaffected

3.5 WHEN the device reports its camera inventory THEN the system SHALL CONTINUE TO use the shipped inventory contract for the Static_Image_Camera entry — fixed id `static-image-camera`, type `StaticImage`, `origin: edge-discovered`, empty `params`, identity under `capabilities.staticImage` — with Defects 1 and 2 requiring no device-side change and no component rebuild, and Defect 3 changing only WHICH entries are reported, never this entry's shape

3.6 WHEN no image is pinned because a removal request was applied THEN the system SHALL CONTINUE TO report the static camera as absent and render the Absent badge from the device-reported `absent` / `absentSince` values, since explicit absence reporting is intended behavior and not part of this defect

3.7 WHEN a workflow carrying an `aravis_camera_source` node bound to the static camera is deployed THEN the system SHALL CONTINUE TO accept the `StaticImage` type at deploy time through the existing compatible set `{Camera, AravisDiscovered, StaticImage}`, unchanged by this frontend-only fix

3.8 WHEN the user opens the "Create camera source" form on the device Cameras tab THEN the system SHALL CONTINUE TO offer exactly the five existing types (Camera (V4L2), NVIDIA CSI, RTSP, Folder, ICam) without StaticImage, and SHALL CONTINUE TO block edit and delete for discovery-managed sources

3.9 WHEN the static-image pin controls are rendered THEN the system SHALL CONTINUE TO gate them on the device-mutation role set (Operator, UseCaseAdmin, PortalAdmin) mirroring the server-side permission, with the same gate helper and role list

3.10 WHEN the Cameras tab is in its normal synced state THEN the system SHALL CONTINUE TO render the cameras table, the conflicts table, and the reachable static-image panel with its existing status, polling, upload-and-pin, replace, and remove behavior, including the loading and load-error early returns

3.11 WHEN the pin shortcut is used from the node panel THEN the system SHALL CONTINUE TO require a reference device first (disabled until one is chosen), stay hidden in manual-entry mode, and open the device page in a new tab with `noopener`, carrying the existing `usecase_id` and `tab=cameras` parameters

3.12 WHEN this fix is delivered THEN the system SHALL CONTINUE TO expose the existing Portal camera and pin routes unchanged (`GET /devices/{id}/cameras`, `GET /devices/{id}/cameras/static-image`, the upload-url and pin routes); Defects 1 and 2 are confined to frontend files and Defect 3 to device-side files, so no Portal backend or infrastructure change ships with either

3.13 WHEN a Pinned_Image exists THEN the device SHALL CONTINUE TO enumerate the static camera locally — `getCameras()` and `rescan_cameras()` still append `Camera(**STATIC_IMAGE_CAMERA_IDENTITY)`, `getCamera("static-image-camera")` still short-circuits to the static handle, and the device `/cameras` route, Image_Source creation, the camera-manager grab path, and local aravis frame feeds are untouched (base spec Requirements 1.2, 2.1-2.3, 2.5-2.7, 3.x, 4.1); the de-duplication happens only in the cloud report

3.14 WHEN a workflow node is already bound to `camera_id: "static-image-camera"` through either duplicate THEN the system SHALL CONTINUE TO resolve the same device-side camera, because both duplicates carry that identical id string (the `arv-` entry's `params.cameraId` and the dedicated entry's `capabilities.staticImage.id`); de-duplicating the registry invalidates no existing binding at runtime

3.15 WHEN any Aravis bus camera other than the static image camera is enumerated THEN the system SHALL CONTINUE TO report it exactly as today — `aravis_stable_id()` unchanged, `enumerate_aravis()` / `_map_camera()` unchanged, the configured-`Camera`-by-`cameraId` merge unchanged, and physical-camera absence tracking unchanged

3.16 WHEN the Aravis Fake camera is enumerated THEN the system SHALL CONTINUE TO report it as a single bindable entry (live-verified on `jetson-thor1`: device `Fake_1` → registry `arv-c9dd20f60ee1`, `AravisDiscovered`, `Aravis Fake`, present, `params.cameraId: "Fake_1"`, binds correctly); it is explicitly NOT a defect, and its absence from the "Create camera source" type list is by design because it is bus-discovered rather than manually created

3.17 WHEN the Static_Image_Camera entry is reported THEN the system SHALL CONTINUE TO use the shipped entry contract unchanged — fixed id, type `StaticImage`, origin `edge-discovered`, `params: {}`, identity plus pin metadata under `capabilities.staticImage`, and the explicit ABSENT entry with a stable `absentSince` after unpin; the existing inventory-presence property keeps holding and only the duplicate `arv-*` entry disappears

3.18 WHEN a configured Image_Source of type `Camera` carries `cameraId: "static-image-camera"` THEN the system SHALL CONTINUE TO merge it with the aravis-enumerated static camera exactly as today (one `cfg-{imageSourceId}` entry with `capabilities.aravis` and the tracked absent state, alongside the dedicated virtual entry); the exclusion applies only to the discovery entry that would otherwise be reported separately, so a user's explicitly configured source is never dropped

3.19 WHEN the Edge_Sync_Agent writes a report THEN the system SHALL CONTINUE TO write the full inventory with unchanged `schemaVersion`, version-counter, `failures`, `acks`, `aliases`, and `discoveryErrors` semantics, and the pin worker's desired-pin handling, markers, and confirmations SHALL CONTINUE TO behave identically — the only differences are the omitted duplicate entry and the one-shot retirement key

3.20 WHEN only the frontend fix has shipped and a device still runs the pre-fix build THEN the system SHALL CONTINUE TO work: both duplicates remain offered and BOTH bind `camera_id` (the `arv-` one from `params.cameraId`, the `StaticImage` one from the new capabilities fallback), so the Portal deploy is safe on its own and needs no device change

## Bug Condition and Property Specification

### Bug Condition

```pascal
FUNCTION isBugCondition(X)
  INPUT: X of type AravisPickerSelection OR PinShortcutArrival
  OUTPUT: boolean

  // Part 1 (id resolution): an entry the Aravis picker offers whose
  // camera id lives in the capabilities block instead of params — the
  // shipped StaticImage inventory shape.
  part1 := X IS AravisPickerSelection
           AND isAravisCompatibleCamera(X.camera) = TRUE
           AND staticImageCapabilityId(X.camera) != NULL
           AND paramsCameraId(X.camera) = NULL

  // Part 2 (pin discoverability): arrival on the device Cameras tab
  // through the node panel's pin shortcut, with the static-image panel
  // out of view below the cameras table.
  part2 := X IS PinShortcutArrival
           AND X.origin = 'pin-static-image-shortcut'
           AND staticImagePanelInView(X.landing) = FALSE

  // Part 3 (duplicate registration, device-side): an inventory merge
  // whose discovery input carries the synthetic static-image bus camera —
  // exactly what getCameras() produces while pinned — reports more than
  // one Camera_Source for the one Static_Image_Camera.
  part3 := X IS InventoryMerge
           AND EXISTS c IN discoveredAravisCameras(X.discoveryResult)
               WHERE c.camera_id = STATIC_IMAGE_CAMERA_ID
           AND NOT referencedByConfiguredSource(X.imageSources, c)
           AND countStaticImageRegistrations(buildInventory(X)) > 1

  // Part 4 (already-published duplicate, migration): a device that once
  // reported the derived arv- key stops reporting it, but shadow merge
  // keeps the key alive, so the Portal never sees the entry go away.
  part4 := X IS ReportWrite
           AND previouslyReported(staticImageAravisStableId())
           AND staticImageAravisStableId() NOT IN reportedCameras(X)
           AND NOT carriesRetirement(X, staticImageAravisStableId())

  RETURN part1 OR part2 OR part3 OR part4
END FUNCTION

FUNCTION staticImageAravisStableId()
  OUTPUT: string

  // Derived, never hardcoded (Requirement 2.11); equals
  // 'arv-6c84191b7fe6' for the shipped identity — recomputed from the
  // constants and matched character-for-character against the live
  // registry payload.
  RETURN aravis_stable_id(
    STATIC_IMAGE_CAMERA_IDENTITY.vendor,
    STATIC_IMAGE_CAMERA_IDENTITY.model,
    STATIC_IMAGE_CAMERA_IDENTITY.serial,
    STATIC_IMAGE_CAMERA_IDENTITY.physical_id)
END FUNCTION

FUNCTION countStaticImageRegistrations(entries)
  INPUT: entries of type LIST OF CameraSourceState
  OUTPUT: integer

  // Registrations of the one virtual camera, counted by the id a node
  // would bind to rather than by entry id — this is what makes the
  // duplicate visible (the existing inventory-presence property counts
  // only camera_source_id = STATIC_IMAGE_CAMERA_ID, so it never saw it).
  RETURN COUNT e IN entries WHERE
    e.camera_source_id = STATIC_IMAGE_CAMERA_ID
    OR e.params.cameraId = STATIC_IMAGE_CAMERA_ID
END FUNCTION

FUNCTION carriesRetirement(report, key)
  INPUT: report of type ReportedDocument, key of type string
  OUTPUT: boolean

  // An explicit shadow deletion: the key present in the reported
  // cameras map with a null value. Nested-map merge semantics make this
  // the only way to remove an already-published key.
  RETURN key IN report.cameras AND report.cameras[key] = NULL
END FUNCTION

FUNCTION staticImageCapabilityId(camera)
  INPUT: camera of type CameraSourceEntry
  OUTPUT: string OR NULL

  // capabilities.staticImage.id as a non-empty string, else NULL.
  RETURN nonEmptyString((camera.capabilities ?? {}).staticImage?.id)
END FUNCTION

FUNCTION paramsCameraId(camera)
  INPUT: camera of type CameraSourceEntry
  OUTPUT: string OR NULL

  RETURN nonEmptyString((camera.params ?? {}).cameraId)
END FUNCTION
```

### Property 1: Fix Checking (Defects 1 and 2, portal frontend)

```pascal
FOR ALL X WHERE isBugCondition(X) DO
  IF X IS AravisPickerSelection THEN
    // Part 1: the capabilities id resolves and is written to the node.
    id ← cameraIdValue'(X.camera)
    ASSERT id = staticImageCapabilityId(X.camera)
    result ← applyAravisCameraSelection'(X.parameters, X.camera, X.deviceId)
    ASSERT result.parameters.camera_id = id
    ASSERT NOT missingRequiredParameter(result.parameters, 'camera_id')
    ASSERT result.hint = bindingHint(X.camera, X.deviceId)
  ELSE
    // Part 2: the pin panel is in view and flagged on arrival.
    ASSERT staticImagePanelInView'(X.landing) = TRUE
  END IF
END FOR
```

**Validates: Requirements 2.1, 2.2, 2.3, 2.4, 2.5, 2.6**

### Property 2: Preservation Checking (Defects 1 and 2, portal frontend)

```pascal
// For all non-buggy inputs — every source whose id already resolves from
// params, every source that resolves no id at all, every non-Aravis
// picker input, and every arrival at the Cameras tab that did not come
// through the pin shortcut — the fixed code behaves identically.
FOR ALL X WHERE NOT isBugCondition(X) DO
  ASSERT F(X) = F'(X)
END FOR

// The offered set is unchanged in both paths: the fallback resolves ids,
// it never widens or narrows picker compatibility.
FOR ALL cameras DO
  ASSERT isAravisCompatibleCamera'(cameras) = isAravisCompatibleCamera(cameras)
END FOR

// The device-side inventory ENTRY SHAPE is untouched by the frontend fix
// (Defect 3 changes which entries are reported, never this shape).
ASSERT staticImageInventoryEntry'() = staticImageInventoryEntry()
```

**Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.8, 3.9, 3.10, 3.11**

### Property 3: Fix Checking (Defect 3, device inventory de-duplication)

```pascal
FOR ALL X WHERE isBugCondition(X) DO
  IF X IS InventoryMerge THEN
    // Part 3: exactly one registration for the one virtual camera —
    // present while pinned, explicitly absent once unpinned after having
    // been reported, none when never reported.
    entries ← buildInventory'(X)
    expected ← 1 IF X.staticImagePinned
               ELSE 1 IF X.staticImageAbsentSince != NULL
               ELSE 0
    ASSERT countStaticImageRegistrations(entries) = expected
    ASSERT NO e IN entries WHERE e.camera_source_id = staticImageAravisStableId()

    // Everything that is not a static-camera registration is byte-for-byte
    // the pre-fix merge output.
    ASSERT withoutStaticRegistrations(entries)
         = withoutStaticRegistrations(buildInventory(X))
  ELSE
    // Part 4: the already-published duplicate is retired exactly once.
    ASSERT carriesRetirement(reportWrite'(X), staticImageAravisStableId())
    ASSERT retirementEmittedCount'(X) = 1  // one-shot, never churning
  END IF
END FOR
```

**Validates: Requirements 2.7, 2.8, 2.9, 2.10, 2.11**

### Property 4: Preservation Checking (Defect 3, device enumeration and reporting)

```pascal
// Every merge whose discovery input carries no aravis camera claiming the
// static camera id is byte-for-byte identical under the fix.
FOR ALL X WHERE NOT EXISTS c IN discoveredAravisCameras(X.discoveryResult)
                 WHERE c.camera_id = STATIC_IMAGE_CAMERA_ID DO
  ASSERT buildInventory'(X) = buildInventory(X)
END FOR

// Device-local enumeration and frame serving are untouched: the static
// camera still enumerates on the bus and still opens by id.
ASSERT getCameras'() = getCameras()
FOR ALL cameraId DO
  ASSERT getCamera'(cameraId) = getCamera(cameraId)
END FOR

// The discovery layer is untouched for every identity, static included.
FOR ALL identity DO
  ASSERT aravis_stable_id'(identity) = aravis_stable_id(identity)
  ASSERT enumerate_aravis'(identity) = enumerate_aravis(identity)
END FOR

// A user's configured Image_Source referencing the static camera still
// merges exactly as today (Requirement 3.18).
FOR ALL X WHERE referencedByConfiguredSource(X.imageSources, staticCamera) DO
  ASSERT configuredEntries(buildInventory'(X)) = configuredEntries(buildInventory(X))
END FOR

// De-duplication cannot invalidate a binding: both duplicates carried the
// same device-side id string.
ASSERT boundCameraId(arvDuplicate) = boundCameraId(dedicatedEntry)
     = STATIC_IMAGE_CAMERA_ID
```

**Validates: Requirements 3.13, 3.14, 3.15, 3.16, 3.17, 3.18, 3.19, 3.20**

**Key Definitions:**
- **F**: `cameraIdValue` / `applyAravisCameraSelection` in `cameraReference.ts`, the pin shortcut
  in `NodeConfigPanel.tsx`, and `DeviceCamerasTab.tsx` (Defects 1 and 2); `build_inventory` in
  `src/backend/camera_sync/inventory.py` and the report write in `src/backend/camera_sync/agent.py`
  (Defect 3) — all as they exist before the fix
- **F'**: the same code after adding the capabilities-backed id fallback, the pin-panel focus
  behavior, the static-camera discovery-entry exclusion, and the one-shot retirement of the
  already-published duplicate key
- **C(X)**: an offered Aravis source whose id lives only in `capabilities.staticImage.id`; an
  arrival at the Cameras tab through the pin shortcut with the panel out of view; an inventory
  merge that reports more than one registration for the one Static_Image_Camera; or a report that
  leaves an already-published duplicate key alive in the shadow
- **P(result)**: `camera_id` populated with the resolved id and the node valid; the pin panel in
  view and flagged; exactly one reported registration for the static camera in every pin state,
  with the already-published duplicate key explicitly deleted once
- **Counterexamples**:
  - the live `jetson-thor1` entry `{type: "StaticImage", params: {}, capabilities: {staticImage:
    {id: "static-image-camera", …}}}` — `cameraIdValue()` returns null,
    `applyAravisCameraSelection()` leaves `camera_id` unset, and the node reports
    `Required parameter 'camera_id' has no value`
  - the live `jetson-thor1` pair `arv-6c84191b7fe6` (`AravisDiscovered`, `AWS-DDA Static Image
    Camera`, `params.cameraId: "static-image-camera"`) AND `static-image-camera` (`StaticImage`,
    `params: {}`) — two registry rows, two absence lifecycles, and two picker options for one
    virtual camera
