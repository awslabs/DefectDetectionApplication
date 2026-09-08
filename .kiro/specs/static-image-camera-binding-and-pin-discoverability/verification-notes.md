# Static Image Camera Binding and Pin Discoverability — Task 5 Live Verification Notes

Spec: `.kiro/specs/static-image-camera-binding-and-pin-discoverability` (bugfix)
Account 164152369890, us-east-1. Portal `https://d23v4ltibogb5x.cloudfront.net`,
rest-api `yqvyoowugk`. Date: 2026-09-08 (all times UTC).

**Scope of this run: READ-ONLY.** Task 5 step (b) pins a test image to `jetson-thor1`,
a real Jetson in use, which mutates live device state. That confirmation was **not**
given, so (b) was skipped entirely, along with the parts of (c)/(d) that need a
present/pinned static camera. No pin request, no upload-url request, and no DELETE on
the pin route were issued. Everything below is a GET, a read-only lambda invoke, or a
static analysis of the served bundle. The device was touched only through two read-only
`curl` calls against its own loopback routes.

- Deploy under test: 2026-09-08 17:19:24–17:24:50Z via `./deploy-frontend.sh`, log
  `edge-cv-portal/deploy-frontend-static-image-camera-binding-20260908T171924Z.out`
  (400 lines); frontend bundle `index-Cdb5gJPC.js` (replaces `index-BE4O437x.js`, which
  the sync deleted from S3 along with its map)
- CloudFront invalidation `I5LJUR3VCXK93X2XXH6HOBW11U` (distribution `E13FEMIUFTIRQ1`,
  bucket `dda-portal-frontend-164152369890`) created 17:20:41Z, status **Completed**
  before any bundle fetch
- Compute deploy (script step 6, flag-less): `EdgeCVPortalComputeStack` ✅ UPDATE_COMPLETE
  (185.07 s; 17:21:23–17:24:44Z). **Zero `AWS::Lambda::Function` deletions, zero
  GroundedSam mentions anywhere in the log** — same shape as the labeling-jobs-epoch-1970
  precedent, second live exercise of the portal-deploy-flag-hardening default-ON path
- `DdaGroundedSamWorker` physical name
  `EdgeCVPortalComputeStack-DdaGroundedSamWorkerA3B13-9paW7gMXvjg2` **unchanged**
  pre/post deploy (LastModified `2026-09-07T17:12:10.007+0000` both times)
- Deployed tree: HEAD `d494945` on `integration/all-specs` plus this fix's working-tree
  diff. `git diff --stat -- edge-cv-portal/`: exactly **5 files**, all frontend — the four
  fix files (`cameraReference.ts`, `NodeConfigPanel.tsx`, `DeviceDetail.tsx`,
  `DeviceCamerasTab.tsx`) plus the one intentional URL-assertion update in
  `NodeConfigPanel.test.tsx` (task 3.2). No `edge-cv-portal/backend/` file, no
  infrastructure file
- Pre-deploy gates (`.kiro/steering/builds.md`): `pgrep -af "gdk component build"` and
  `pgrep -af "build-custom.sh"` both empty, re-checked immediately before launching
- Analysis artifacts: `/tmp/staticcam-verify/` on the verification host (served bundle
  copy, synthesized event, raw lambda response, resolution proof)

## 1. Method — synthesized-event precedent

Browser UI automation unavailable; deployed behavior verified per the
labeling-jobs-epoch-1970-dates / grounded-sam-prompt-tuning-preview precedent:
(a) served-bundle content checks via CloudFront, (b) direct invoke of the **real
deployed** camera-registry handler with a synthesized API-Gateway-shaped event through
the handler's own auth path (`requestContext.authorizer.claims`:
`sub = a4b804e8-5061-7004-12f2-38a0149dcd4c`, `custom:role = PortalAdmin`,
email/username fillers), `pathParameters.id = jetson-thor1`,
`queryStringParameters.usecase_id = 645504ce-a60a-4009-8349-7548c0025cd3`, and
(c) a data-level replay of the fixed vs. old resolution logic over the live payload.

Routing note: `GET /devices/{id}/cameras` is served by **`CameraRegistryHandler`**
(`camera_registry.handler`) — physical name
`EdgeCVPortalComputeStack-CameraRegistryHandler6726-8wszcxZw5oy0`, LastModified
`2026-09-08T17:23:00Z` (this deploy's env-var update).

**Why a data-level replay is the right proof here.** Both defects are pure frontend
logic; the fix changes no request and no response. The served bundle carries the logic
(§2) and the live payload carries the input (§3), so applying the shipped resolution
rule to the shipped payload proves what the picker now writes into `camera_id` without
mutating anything. The remaining gap — a human clicking through the Workflow_Builder —
is called out in §6.

## 2. Deployed bundle carries both fixes

`GET https://d23v4ltibogb5x.cloudfront.net/index.html` (cache-busted) references exactly
`assets/index-Cdb5gJPC.js` + `assets/index-BcxTwsXV.css`. Served JS (2,096,676 bytes) is
**byte-identical** to the locally built `frontend/dist/assets/index-Cdb5gJPC.js`:
sha256 `6d83bf3d6f6b88718bd21df4eed0003a7d02b2c04328dd5dcadcdf040b094ef2` both sides.

### (i) StaticImage-gated capabilities fallback, adjacent to the `params.cameraId` resolution

Served, minified (helper `Oae` = `staticImageCapabilityId`, `Wj` = `cameraIdValue`,
`zae` = `applyAravisCameraSelection`):

```js
function Oae(e){const t=(e.capabilities??{}).staticImage;
  if(t==null||typeof t!="object"||Array.isArray(t))return null;
  const n=t.id;return typeof n=="string"&&n!==""?n:null}
function Wj(e){const t=(e.params??{}).cameraId;
  return typeof t=="string"&&t!==""?t:e.type==="StaticImage"?Oae(e):null}
function zae(e,t,n){const r={...e},s=Wj(t);s!==null&&(r.camera_id=s);
  const o=t.params??{};typeof o.gain=="number"&&(r.gain=o.gain),
  typeof o.exposure=="number"&&(r.exposure=o.exposure)…
```

Every clause of Requirement 2.1 and 3.1 is visible in the shipped artifact:
`params.cameraId` resolved **first** and unchanged; the fallback gated on
`e.type==="StaticImage"` (so the offered set cannot widen — Req 3.4); the id read from
the capabilities **block** rather than compared against a hardcoded
`'static-image-camera'` (Req 2.11-style derivation, so a future identity change flows
through); and the external-input guards (`null`/non-object/array block, non-string or
empty `id` → `null`, Req 3.2). `zae` still writes `camera_id` only when the resolution is
non-null and still copies `gain`/`exposure` only as numbers (Req 3.3). The bundle contains
exactly **one** `params??{}).cameraId` occurrence, i.e. a single `cameraIdValue`
definition — the old params-only form is gone, not shadowed.

`isAravisCompatibleCamera` ships **unchanged** (Req 3.4), verbatim from the bundle:

```js
function d6(e){return e.type==="AravisDiscovered"||e.type==="StaticImage"?!0:e.type==="Camera"&&Wj(e)!==null}
```

Same three arms as before the fix; the only difference reachable through `Wj` is in the
`Camera` arm, and the `type==="StaticImage"` gate inside `Wj` makes that arm's behavior
identical (a `Camera` entry never takes the fallback). This is the shipped function the
§4 offered-set comparison replays.

### (ii) `focus=static-image` shortcut parameter

```js
const o6="focus",a6="static-image"
…onClick:()=>{…const T=new URLSearchParams;
  l&&T.set("usecase_id",l),T.set("tab","cameras"),T.set(o6,a6),
  window.open(`/devices/${encodeURIComponent(v)}?${T.toString()}`,"_blank","noopener")},
  "data-testid":"pin-static-image-shortcut"
```

The new parameter ships alongside the preserved `usecase_id` and `tab=cameras`, the
`noopener` new-tab open, and the `disabled:v===null` until-a-device-is-chosen gate
(Req 3.11).

Reader side, `DeviceDetail` → Cameras tab (`Kae` = `DeviceCamerasTab`):

```js
{id:"cameras",label:"Cameras",content:i.jsx(Kae,{deviceId:l.device_id,usecaseId:E||"",focusStaticImage:C})}
function Kae({deviceId:e,usecaseId:t,focusStaticImage:n=!1}){…}
```

Plumbed as an **optional** prop defaulting to `false`, so every other call site renders
exactly as before (task 3.2's no-router-dependency constraint).

### (iii) `static-image-focus-flag` test id, and the one-shot scroll

```js
children:[r&&i.jsx(k,{margin:{bottom:"m"},children:i.jsx(ie,{type:"info",
  "data-testid":"static-image-focus-flag",header:"Pin a static test image here",…
```

```js
const z=u.useRef(null),D=u.useRef(!1);
u.useEffect(()=>{var J;if(!n||c||f!==null||D.current)return;
  const q=z.current;q!==null&&(D.current=!0,(J=q.scrollIntoView)==null||J.call(q,{block:"start"}))},[n,c,f])
```

The scroll fires only with the flag set, only after loading resolves and no load error,
and at most **once** per arrival (`D.current`), via an optional call — Requirement 2.5
end to end. The Req 2.6 create-form pointer also ships:
`data-testid="camera-form-static-image-note"` present.

### Preservation visible in the same bundle

- Create-form type options are still exactly five, StaticImage absent (Req 3.8):
  `[{label:"Camera (V4L2)",value:"Camera"},{label:"NVIDIA CSI",value:"NvidiaCSI"},{label:"RTSP",value:"RTSP"},{label:"Folder",value:"Folder"},{label:"ICam",value:"ICam"}]`
  — zero matches for any `value:"StaticImage"` option
- `focusStaticImage` defaults to `false`, so arrival without the flag is the pre-fix
  render (Req 3.10)

## 3. Live payload — the exact duplicate from bugfix.md 1.8/1.9, verbatim

`aws lambda invoke` on `CameraRegistryHandler…-8wszcxZw5oy0` with the synthesized event
→ **200**, `state: synced`, `device_status: HEALTHY`, **`count: 9`**,
`last_report_at: 1788839399575` (2026-09-08T03:49:59Z).

The two static-image rows, verbatim from the live response:

```json
{ "camera_source_id": "arv-6c84191b7fe6", "type": "AravisDiscovered",
  "name": "AWS-DDA Static Image Camera", "origin": "edge-discovered",
  "absent": true, "absent_since": 1788800043461, "version": 4,
  "params": { "address": "internal", "cameraId": "static-image-camera",
              "protocol": "StaticImage", "serial": "STATIC-IMAGE-0" },
  "capabilities": { "aravis": { "vendor": "AWS-DDA", "model": "Static Image Camera",
              "serial": "STATIC-IMAGE-0", "physicalId": "static-image-camera",
              "address": "internal", "protocol": "StaticImage" } } }

{ "camera_source_id": "static-image-camera", "type": "StaticImage",
  "name": "Static Image Camera", "origin": "edge-discovered",
  "absent": true, "absent_since": 1788839397466, "version": 6,
  "params": {},
  "capabilities": { "staticImage": { "id": "static-image-camera", "vendor": "AWS-DDA",
              "model": "Static Image Camera", "serial": "STATIC-IMAGE-0",
              "physicalId": "static-image-camera", "address": "internal",
              "protocol": "StaticImage" } } }
```

`absent_since: 1788839397466` on the dedicated entry matches task 1's recorded
counterexample **character-for-character**, and `arv-6c84191b7fe6` matches the id task 6
recomputed from the shipped constants through `aravis_stable_id()`. Both are `absent`
(nothing pinned), which is intended behavior, not a defect (Req 3.6).

**Nine cameras and TWO static-image rows is CORRECT for this deploy.** Defect 3 (the
duplicate registration) is device-side: it lands only when the user's Greengrass
component build ships the `src/backend/camera_sync/` fix (task 10). This portal deploy
neither fixes nor hides the duplicate, by design — bugfix.md 3.20 requires the frontend
to be safe on its own with a pre-fix device, which §4 proves.

## 4. Data proof — fixed vs. old resolution over the live payload

Both oracles reimplemented from the shipped TS (old = `params.cameraId` only;
fixed = params first, then the StaticImage-gated `capabilities.staticImage.id`) and run
over all nine live entries:

| `camera_source_id` | type | offered? | OLD `cameraIdValue` | FIXED `cameraIdValue` |
|---|---|---|---|---|
| `arv-6c84191b7fe6` | AravisDiscovered | yes | `'static-image-camera'` | `'static-image-camera'` |
| `arv-c9dd20f60ee1` | AravisDiscovered | yes | `'Fake_1'` | `'Fake_1'` |
| `arv-797b019251e9` | AravisDiscovered | yes | `'Basler-26760165225D-23405149'` | `'Basler-26760165225D-23405149'` |
| `arv-cf582dea7590` | AravisDiscovered | yes | `'Basler-267601652282-23405186'` | `'Basler-267601652282-23405186'` |
| `cfg-28183exv` | Camera | yes | `'Basler-26760165225D-23405149'` | `'Basler-26760165225D-23405149'` |
| `cfg-o70qz7ci` | Camera | yes | `'Basler-267601652282-23405186'` | `'Basler-267601652282-23405186'` |
| **`static-image-camera`** | **StaticImage** | **yes** | **`None` ← the defect** | **`'static-image-camera'` ← fixed** |
| `cfg-iebllnt4` | Folder | no | `None` | `None` |
| `cfg-pgc367hy` | Folder | no | `None` | `None` |

- **Requirements 2.1, 2.2, 2.3 (data level).** The live `static-image-camera` entry —
  `type: StaticImage`, `params: {}`, `capabilities.staticImage.id: 'static-image-camera'`
  — resolved `None` under the old logic (hence `Current value: (not set)` and
  `Required parameter 'camera_id' has no value`) and resolves `'static-image-camera'`
  under the shipped logic. `applyAravisCameraSelection` writes exactly that value, so the
  violation is structurally impossible for this entry.
- **bugfix.md 3.20 — both currently-offered options now bind the SAME id.**
  `arv-6c84191b7fe6` → `'static-image-camera'` from `params.cameraId`;
  `static-image-camera` → `'static-image-camera'` from the capabilities fallback.
  The resolved-id set across both rows is `{'static-image-camera'}`, size 1. Whichever
  duplicate the user picks binds the same device-side camera, so the portal deploy is
  safe against a pre-fix device and no existing binding is invalidated (Req 3.14).
- **Requirement 3.4 — the offered set did not move.** Running
  `isAravisCompatibleCamera` with the old resolver and with the fixed resolver over the
  live payload returns the identical seven ids in the identical order
  (`arv-6c84191b7fe6`, `arv-c9dd20f60ee1`, `arv-797b019251e9`, `arv-cf582dea7590`,
  `cfg-28183exv`, `cfg-o70qz7ci`, `static-image-camera`); the two `Folder` sources stay
  out of both. The fallback resolves ids, it does not widen or narrow compatibility.
- **Requirement 3.1 — params-first preserved on every other row.** All six non-static
  offered entries resolve byte-identically under both oracles, from `params.cameraId`.

## 5. Healthy Aravis Fake camera unchanged (Req 3.16)

Live entry, verbatim:

```json
{ "camera_source_id": "arv-c9dd20f60ee1", "type": "AravisDiscovered",
  "name": "Aravis Fake", "origin": "edge-discovered", "absent": false,
  "absent_since": null, "stale": false, "sync_status": "synced", "version": 1171,
  "params": { "cameraId": "Fake_1", "serial": "1", "address": "0.0.0.0", "protocol": "Fake" },
  "capabilities": { "aravis": { "vendor": "Aravis", "model": "Fake", "serial": "1",
              "physicalId": "Fake_1", "address": "0.0.0.0", "protocol": "Fake" } } }
```

Single entry, **present** (`absent: false`), `params.cameraId: 'Fake_1'` — matches the
live-healthy baseline in bugfix.md 3.16 exactly. The fixed logic resolves `'Fake_1'`
**from params**, never from capabilities: this entry carries no `staticImage` capability
block at all (`'staticImage' in capabilities` → `False`), and even if it did, the
`type === 'StaticImage'` gate would skip the fallback. Absence badges elsewhere still
render from device-reported `absent`/`absent_since` (Req 3.6).

## 6. Device read-only spot-check (no mutation)

Over SSH to `jetson-thor1`, two GETs against the device's own loopback routes. Nothing
pinned, unpinned, restarted, or modified:

```
GET http://127.0.0.1:5000/cameras
[{"id":"Fake_1","model":"Fake","address":"0.0.0.0","physical_id":"Fake_1",
  "protocol":"Fake","serial":"1","vendor":"Aravis"},
 {"id":"Basler-26760165225D-23405149","model":"acA4600-10uc","address":"USB3",
  "physical_id":"26760165225D","protocol":"USB3Vision","serial":"23405149","vendor":"Basler"}]

GET http://127.0.0.1:5000/static-image-camera/pin
{"pinned":false,"cameraId":"static-image-camera","metadata":null}
```

- `pinned: false` — nothing is pinned, consistent with both registry rows reporting
  `absent: true`. This is exactly why step (b) was needed to exercise the present-camera
  path, and exactly what was skipped
- The static camera is correctly **absent from the device's bus enumeration** while
  unpinned: `getCameras()` appends `Camera(**STATIC_IMAGE_CAMERA_IDENTITY)` only while a
  Pinned_Image exists (Req 3.13). The `Fake_1` + Basler enumeration is the expected
  unpinned shape
- Observation, out of scope, not a defect claim: the device currently enumerates **one**
  Basler while the registry snapshot (`last_report_at` 2026-09-08T03:49:59Z, ~13.7 h old)
  carries two present Basler entries. That is device-state drift since the last report,
  independent of this spec; no absence lifecycle in this fix's scope depends on it

## 7. Compute changeset — nothing unexpected, and the device diff could not reach it

Resource events on `EdgeCVPortalComputeStack`, by type and status:

| resource type | UPDATE_IN_PROGRESS | UPDATE_COMPLETE | DELETE |
|---|---|---|---|
| `AWS::Lambda::Function` | 41 | 41 | **0** |
| `AWS::CloudFormation::Stack` (nested) | 7 | 13 (+1 CLEANUP) | 0 |
| `AWS::CloudFormation::CustomResource` | 4 | 2 | 2 |
| `AWS::Lambda::LayerVersion` | 2 | 1 | 1 |

Zero `CREATE_IN_PROGRESS` events anywhere. The changeset was: the 41 handler functions
updated in place (CloudFront-domain env var + new SharedLayer version binding), the six
nested API stacks plus the API Gateway nested stack updated, and three replaced
resources whose old physical resources were removed in the cleanup phase — the two
custom resources `LambdaEnvUpdater/Default` and `SageMakerEventBridgeIntegration/Default`
and the previous `SharedLayer` `LayerVersion`. Those three are the only DELETE events
(6 lines = 3 × in-progress + complete) and they are the same custom-resource churn the
epoch-1970 deploy recorded. **No function was deleted, and `GroundedSam` appears zero
times in the 400-line log** (§ worker check above confirms `-9paW7gMXvjg2` survived
byte-for-byte).

The device-track diff (`src/backend/camera_sync/agent.py`, `inventory.py`) is in the
working tree but **cannot** reach the portal through this deploy: those modules are
LocalServer component code, and neither
`edge-cv-portal/infrastructure/lib/**` nor `edge-cv-portal/backend/**/*.py` references
`src/backend/camera_sync` or even the name `camera_sync` (both greps: no matches). The
compute step bundles only `edge-cv-portal/backend/` sources.

## Verdict

Everything in the read-only path passed, nothing retried, no failure categories observed:

- Frontend deployed 17:19:24–17:24:50Z; invalidation **Completed**; served bundle
  `index-Cdb5gJPC.js` byte-identical to the local build (sha256 match)
- All three required markers present in the SERVED bundle: the StaticImage-gated
  `capabilities.staticImage.id` fallback adjacent to the unchanged `params.cameraId`
  resolution, the `focus=static-image` shortcut parameter (with its reader plumbed as an
  optional prop), and the `static-image-focus-flag` arrival alert — plus the Req 2.6
  create-form pointer, the one-shot guarded `scrollIntoView`, and the unchanged
  five-option type list
- Live inventory: 9 cameras, both static-image rows reproduced verbatim, `absent_since`
  and the derived `arv-6c84191b7fe6` id matching the spec's recorded values exactly
- Fixed-vs-old proof on live data: the `StaticImage` entry moves from `None` (the defect)
  to `'static-image-camera'`; the `AravisDiscovered` duplicate stays
  `'static-image-camera'`; **both bind the same id** (3.20); the offered set is
  bit-identical under both oracles (3.4); all six other offered entries resolve
  identically from params (3.1)
- `Aravis Fake` `arv-c9dd20f60ee1` present and unchanged, resolving `'Fake_1'` from
  params with no `staticImage` capability block anywhere near it (3.16)
- `DdaGroundedSamWorker` `…-9paW7gMXvjg2` survived the flag-less compute deploy unchanged
- Backend and infrastructure untouched (5-file frontend diff; changeset is env-var
  updates plus custom-resource churn only)
- Device read-only: `pinned: false`, `Fake_1` + one Basler enumerated, static camera
  correctly out of the unpinned bus enumeration; no device state altered

Requirements exercised live: 2.1, 2.2, 2.3 (capabilities resolution + `camera_id`
population, via served bundle logic × live payload), 2.4 (option description fed by the
same resolved id), 2.5 (focus parameter, plumbing, guarded scroll, arrival flag in the
served bundle), 2.6 (create-form pointer shipped, StaticImage still not creatable),
3.1 (params-first on all six other offered entries), 3.4 (offered set unchanged over
live data), 3.6 (absence still device-reported), 3.8 (five type options), 3.10/3.11
(optional-prop default preserves the existing render and the shortcut's gating),
3.12 (no backend or infra change), 3.13 (device enumeration untouched — unpinned shape
confirmed on hardware), 3.14/3.20 (both duplicates bind the same id), 3.16 (Fake camera
healthy and unchanged).

## What remains UNVERIFIED

Two gaps, both by design of this run, neither indicating a problem:

1. **Pending the user's pin approval** (task 5 step b, and the parts of c/d that need a
   present camera). Not exercised, because pinning mutates live state on a Jetson in use:
   - the pin round-trip itself (upload-url → pin → status reporting
     `deviceReported.present: true` with the static camera no longer absent)
   - the **present**-camera variant of the picker: with `pinned: false` today, both
     static rows are `absent`, so the binding proof in §4 is a data-level replay of the
     shipped logic over the shipped payload rather than a click-through. The resolution
     rule reads only `type`, `params`, and `capabilities` — none of which changes shape
     when the camera becomes present — so the outcome is determined, but it has not been
     observed in a browser
   - the human-visible confirmations: the panel actually rendering
     `camera_id = static-image-camera` instead of `(not set)`, the
     `Required parameter 'camera_id' has no value` violation actually disappearing from
     the node, and the Cameras tab visibly landing on the flagged static-image panel with
     the pin controls reachable past the 9-row table (step d). All three are asserted by
     the task 1/2/3 test suites and all three ship in the verified bundle
2. **Pending the user's Greengrass component build** (device track, tasks 6-10, ~100
   min + a deployment revision). Until it lands, `jetson-thor1` keeps reporting the
   duplicate: 9 cameras, two static-image rows, two options in the picker. That is the
   expected and correct state for a frontend-only deploy — §4 shows both options bind
   identically, so the duplicate is cosmetic, not a dead end. Requirements 2.7-2.10 (one
   registration per pin state) and the one-shot shadow-key retirement (2.9, 2.11) are
   untested against live hardware and remain the subject of task 10

Cleanup: none required — artifacts confined to `/tmp/staticcam-verify/`; no portal state
and no device state created or mutated (read-only invokes and GETs only).
