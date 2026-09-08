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

> **Amendment (17:37–17:43Z, same day).** The user subsequently **approved pinning a test
> image**, so step (b) and the present-camera parts of (c)/(d) were completed in a second
> pass. Sections 1-7 below remain exactly as recorded and remain read-only; the pinned run
> is **§8**, and the image is intentionally **left pinned**. The "What remains UNVERIFIED"
> section at the end has been updated accordingly.

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

## 8. Pinned-path verification (user-approved live pin)

**Scope of this section: MUTATING, with explicit user approval.** Task 5 step (b) plus the
present-camera parts of (c)/(d), run 17:37:29–17:43:07Z against the same deploy verified in
§§1-7 (bundle re-fetched mid-run and confirmed unchanged, below). One pin request was
submitted through the Portal API; **the image is deliberately left PINNED** — no
`DELETE .../pin`, no unpin, no restart, no config change, no source-file change, no build,
no deploy. Artifacts under `/tmp/staticcam-verify/pin/`.

### 8.1 Test image

Generated locally with Pillow 11.3.0 (deep-blue field, yellow border, red disc, white
diagonals, three text labels — visually unmistakable against the previous 320×240 pins):

| property | value |
|---|---|
| file name | `dda-static-pin-test-640x480.png` |
| format | `PNG` (RGB, decoded and fully loaded to verify) |
| dimensions | 640 × 480 |
| size | 12,063 bytes (0.02 % of the 50 MB `MAX_PIN_IMAGE_BYTES` limit) |
| sha256 | `83cca4bb036a4faef925a54b8b34e784a7d04e7b67dc1079ba6e139fc9ea5205` |

**Chain of custody closed end to end.** The `PIN_REQUEST#01788889080505#55fe9832` item in
`dda-portal-camera-registry` records `sha256 = 83cca4bb036a4faef925a54b8b34e784a7d04e7b67dc1079ba6e139fc9ea5205`,
`size_bytes = 12063`, `format = PNG`, `file_name = dda-static-pin-test-640x480.png`,
`s3_key = static-image-pins/jetson-thor1/01788889080505#55fe9832` — the hash the Portal
computed over the staged bytes is **identical** to the local file's, and the device echoed
back 640 × 480 / PNG / 12,063 / same file name. The bytes that reached the device are the
bytes generated here.

### 8.2 Pin lifecycle and timings

Same synthesized-event method as §1 (real deployed `CameraRegistryHandler…-8wszcxZw5oy0`,
`requestContext.authorizer.claims` PortalAdmin path, `pathParameters.id = jetson-thor1`,
`queryStringParameters.usecase_id = 645504ce-a60a-4009-8349-7548c0025cd3`).

| # | step | time (UTC) | outcome |
|---|---|---|---|
| 0 | `GET /cameras/static-image` (pre-pin baseline) | 17:37:29 | 200; `latest` = `remove` / **applied** (`01788839397371#6af1b999`); `deviceReported {present: false, absent: true, absentSince: 1788839397466}`; 13 history records |
| 1 | `POST /cameras/static-image/upload-url` | 17:37:41.276 | **200**; `stagingKey static-image-pins/staging/f92b1b8d08484e00835bd39e56cb71ab`, `bucket dda-component-us-east-1-164152369890`, `expiresInSeconds 900` |
| 2 | `PUT` presigned URL | 17:37:52.871 | **HTTP 200**, 12,063 bytes uploaded |
| 3 | `POST /cameras/static-image/pin` (`stagingKey` + `fileName`) | 17:37:59.283 → returned 17:38:00.853 | **201**; `pinRequestId 01788889080505#55fe9832`, `status pending` |
| 4 | device confirmation (recorded on the item) | `createdAt 1788889080505` → `completedAt 1788889081197` | **applied in 692 ms**; `deviceMetadata.pinnedAtEpochMs 1788889081193` |
| 5 | `GET /cameras/static-image` poll #1 | 17:38:11 | already **terminal**: `applied`, no `failureReason`, `connectivity` correctly absent (only emitted while pending) |
| 6 | `GET /cameras/static-image` | 17:38:43 | **`deviceReported {present: true, absent: false}`**, `absentSince` gone — ~42 s after submit |
| 7 | device SSH read-only checks | 17:38:51 | see §8.3 |
| 8 | `GET /devices/jetson-thor1/cameras` | 17:39:07 | dedicated row **present** (v7, report `1788889081218`); `arv-` duplicate still `absent` (v4) |
| 9 | `GET /devices/jetson-thor1/cameras` | 17:40:33 | `arv-` duplicate **present** (v5, report `1788889153915` = 17:39:13.915Z) |
| 10 | served-bundle re-fetch | 17:41:22 | `index-Cdb5gJPC.js`, sha256 `6d83bf3d…094ef2` — **unchanged** from §2 |
| 11 | `GET /cameras/static-image` (final) | 17:43:07 | `applied` + `deviceReported {present: true, absent: false}`; **left pinned** |

**Elapsed:** submit → `applied` **0.69 s** (device round trip); submit → portal-visible
`deviceReported.present: true` **~42 s**; submit → *both* registry rows present **~2 min
34 s** wall (the underlying device report landed at 17:39:13.9Z, 73 s after submit).
No `failed` status at any point, so no retry was needed and none was attempted.

Terminal `latest`, verbatim:

```json
{ "pinRequestId": "01788889080505#55fe9832", "op": "pin", "status": "applied",
  "createdAt": 1788889080505, "completedAt": 1788889081197,
  "deviceMetadata": { "width": 640, "height": 480, "format": "PNG",
                      "fileName": "dda-static-pin-test-640x480.png",
                      "fileSizeBytes": 12063, "pinnedAtEpochMs": 1788889081193 } }
```

Two staged-lifecycle details worth recording, both pre-existing designed behavior:
the pre-pin baseline still carried `deviceMetadata` from the *previous* pin
(`race2-a.png`, 320 × 240) even though the latest op was an applied `remove` — that is
`build_status_view`'s "most recent **pin**-type request, if applied" rule (Req 1.7), not a
staleness bug; and history grew 13 → 14 with the new request at the head, superseding
nothing (no request was pending).

### 8.3 Device-side confirmation (read-only, over SSH)

Two loopback GETs at 17:38:51Z. Nothing written, restarted, or reconfigured:

```
GET http://127.0.0.1:5000/static-image-camera/pin
{"pinned":true,"cameraId":"static-image-camera",
 "metadata":{"fileName":"dda-static-pin-test-640x480.png","format":"PNG",
             "width":640,"height":480,"fileSizeBytes":12063,
             "pinnedAtEpochMs":1788889081193}}

GET http://127.0.0.1:5000/cameras
[{"id":"Fake_1","model":"Fake","address":"0.0.0.0","physical_id":"Fake_1",
  "protocol":"Fake","serial":"1","vendor":"Aravis"},
 {"id":"Basler-26760165225D-23405149","model":"acA4600-10uc","address":"USB3",
  "physical_id":"26760165225D","protocol":"USB3Vision","serial":"23405149","vendor":"Basler"},
 {"id":"static-image-camera","model":"Static Image Camera","address":"internal",
  "physical_id":"static-image-camera","protocol":"StaticImage","serial":"STATIC-IMAGE-0",
  "vendor":"AWS-DDA"}]
```

- `pinned: true` with **metadata matching the pinned image field for field** — file name,
  `PNG`, 640 × 480, 12,063 bytes — and `cameraId: static-image-camera`
- **Requirement 3.13 proven on hardware.** `/cameras` now enumerates
  `static-image-camera` **alongside** `Fake_1` and the Basler, exactly the third element
  `getCameras()` appends while a Pinned_Image exists. §6 recorded the complementary
  unpinned shape (two entries, no static camera) on the same device four minutes earlier,
  so both halves of the enumeration contract are now observed live. This is the behavior
  the device fix deliberately leaves alone — de-duplication happens only in the cloud
  report, never in device-local enumeration
- The single-Basler enumeration noted in §6 persists (registry has two present Basler
  rows); unchanged by the pin, out of this spec's scope, not a defect claim

### 8.4 Portal registry — the pre-fix PINNED baseline for task 10

`GET /devices/jetson-thor1/cameras` at 17:40:33Z: `state synced`, `device_status HEALTHY`,
**`count: 9`** (unchanged from §3 — the pin flips absence, it does not add rows),
`last_report_at 1788889153915`. Every row present, none stale:

| `camera_source_id` | type | absent | version | name |
|---|---|---|---|---|
| `arv-6c84191b7fe6` | AravisDiscovered | **false** | 5 | AWS-DDA Static Image Camera |
| `arv-c9dd20f60ee1` | AravisDiscovered | false | 1171 | Aravis Fake |
| `arv-797b019251e9` | AravisDiscovered | false | 1 | Basler acA4600-10uc |
| `arv-cf582dea7590` | AravisDiscovered | false | 2 | Basler acA4600-10uc |
| `cfg-28183exv` | Camera | false | 9 | Basler-26760165225D-23405149 |
| `cfg-o70qz7ci` | Camera | false | 860 | Basler-267601652282-23405186 |
| **`static-image-camera`** | **StaticImage** | **false** | 7 | Static Image Camera |
| `cfg-iebllnt4` | Folder | false | 2 | cookies |
| `cfg-pgc367hy` | Folder | false | 2 | yolotest |

Both static-image rows verbatim from the live response — **this is the pre-fix pinned
baseline task 10 re-checks after the user's component build**:

```json
{ "camera_source_id": "arv-6c84191b7fe6", "name": "AWS-DDA Static Image Camera",
  "type": "AravisDiscovered", "origin": "edge-discovered", "version": 5,
  "last_reported_at": 1788889153915, "sync_status": "synced",
  "absent": false, "stale": false,
  "params": { "serial": "STATIC-IMAGE-0", "cameraId": "static-image-camera",
              "protocol": "StaticImage", "address": "internal" },
  "capabilities": { "aravis": { "address": "internal", "protocol": "StaticImage",
              "serial": "STATIC-IMAGE-0", "vendor": "AWS-DDA",
              "model": "Static Image Camera", "physicalId": "static-image-camera" } } }

{ "camera_source_id": "static-image-camera", "name": "Static Image Camera",
  "type": "StaticImage", "origin": "edge-discovered", "version": 7,
  "last_reported_at": 1788889153915, "sync_status": "synced",
  "absent": false, "stale": false,
  "params": {},
  "capabilities": { "staticImage": { "id": "static-image-camera",
              "physicalId": "static-image-camera", "model": "Static Image Camera",
              "vendor": "AWS-DDA", "address": "internal", "protocol": "StaticImage",
              "serial": "STATIC-IMAGE-0",
              "width": 640, "height": 480, "format": "PNG",
              "fileName": "dda-static-pin-test-640x480.png", "fileSizeBytes": 12063,
              "pinnedAtEpochMs": 1788889081193 } } }
```

What changed against the §3 (unpinned) snapshot:

- **Dedicated `static-image-camera` row: `absent: true` → `absent: false`, `absent_since`
  key gone, version 6 → 7**, and the pin metadata **folded into
  `capabilities.staticImage`** next to the fixed identity: `width 640`, `height 480`,
  `format PNG`, `fileName dda-static-pin-test-640x480.png`, `fileSizeBytes 12063`,
  `pinnedAtEpochMs 1788889081193`. `params` stays **`{}`** — the shipped inventory
  contract is unchanged by the pin, which is precisely why the frontend fix has to read
  the id from `capabilities.staticImage.id`
- **`arv-6c84191b7fe6` duplicate: `absent: true` → `absent: false`, `absent_since` gone,
  version 4 → 5.** Its `params.cameraId` / `capabilities.aravis` block is byte-identical
  to §3
- `count` stayed 9; the other seven rows are unchanged

**The duplicate flipped one report LATE, and that is worth knowing for task 10.** The
17:38:01.218Z report (triggered by the pin confirmation) carried the dedicated entry
present but still showed the `arv-` row absent, because the agent reports against
`self._discovery.latest_snapshot` — a cache refreshed on the discovery service's own
re-enumeration cycle (`DEFAULT_INTERVAL_SECONDS = 300`, overridable via
`CameraDiscoveryIntervalSeconds`). The duplicate only appears once the bus is
re-enumerated and `getCameras()` returns the appended static camera; here that happened
at 17:39:13.915Z, 73 s after the pin. So **the pinned state converges in two steps
pre-fix**: dedicated row immediately, `arv-` duplicate at the next discovery scan.
Post-build (task 10) the second step must produce **no** change to any `arv-6c84191b7fe6`
row at all — and the key must be retired from the shadow with an explicit null exactly
once — so task 10 should re-read the inventory **at least one full discovery interval
after the pin**, not just seconds after it, or it risks reading a green result that only
reflects the stale snapshot.

**Task 10 comparison target, stated explicitly.** Pinned, pre-fix (now): `count: 9`,
**two** present static-image rows (`arv-6c84191b7fe6` AravisDiscovered + dedicated
`static-image-camera` StaticImage), two Aravis-picker options for one camera. Pinned,
post-fix (expected): **`count: 8`**, exactly **one** present static-image row
(`static-image-camera`, carrying the pin metadata), **zero** rows with
`camera_source_id == "arv-6c84191b7fe6"` — deleted by the Portal's existing
missing-from-report path once the agent retires the shadow key — and one picker option.

### 8.5 Binding proof on the PRESENT payload

The served bundle was re-fetched at 17:41:22Z and is still `index-Cdb5gJPC.js`, sha256
`6d83bf3d6f6b88718bd21df4eed0003a7d02b2c04328dd5dcadcdf040b094ef2`, with the three
shipped functions re-extracted verbatim (`Oae` = `staticImageCapabilityId`,
`Wj` = `cameraIdValue`, `zae` = `applyAravisCameraSelection`, `d6` =
`isAravisCompatibleCamera`; still exactly **one** `params??{}).cameraId` occurrence). The
§2 listings therefore still describe the running code. Those functions were transliterated
into oracles (`/tmp/staticcam-verify/pin/binding_replay.py`) and applied to the
**now-present** 9-entry payload, with the required-parameter check mirroring
`checkParameterValue()` in `parameters.ts` for a required `camera_id` string:

| `camera_source_id` | type | absent | offered | OLD `cameraIdValue` | FIXED `cameraIdValue` | `camera_id` after apply | violation |
|---|---|---|---|---|---|---|---|
| `arv-6c84191b7fe6` | AravisDiscovered | false | yes | `'static-image-camera'` | `'static-image-camera'` | `'static-image-camera'` | none |
| `arv-c9dd20f60ee1` | AravisDiscovered | false | yes | `'Fake_1'` | `'Fake_1'` | `'Fake_1'` | none |
| `arv-797b019251e9` | AravisDiscovered | false | yes | `'Basler-26760165225D-23405149'` | same | same | none |
| `arv-cf582dea7590` | AravisDiscovered | false | yes | `'Basler-267601652282-23405186'` | same | same | none |
| `cfg-28183exv` | Camera | false | yes | `'Basler-26760165225D-23405149'` | same | same | none |
| `cfg-o70qz7ci` | Camera | false | yes | `'Basler-267601652282-23405186'` | same | same | none |
| **`static-image-camera`** | **StaticImage** | **false** | **yes** | **`None`** | **`'static-image-camera'`** | **`'static-image-camera'`** | **none** |
| `cfg-iebllnt4` | Folder | false | no | `None` | `None` | absent | `V4_MISSING_REQUIRED_PARAMETER` |
| `cfg-pgc367hy` | Folder | false | no | `None` | `None` | absent | `V4_MISSING_REQUIRED_PARAMETER` |

The two `Folder` rows are **not offered** by the picker, so their violation is
unreachable — it is what a source the picker never presents would produce, listed only for
completeness.

Detail on the two static-image entries, applied over a prior parameter record
`{gain: 5, exposure: 100}`:

```
static-image-camera  (StaticImage, absent=false)
  resolved via   capabilities.staticImage.id   (the fixed fallback)
  parameters     {"camera_id": "static-image-camera", "exposure": 100, "gain": 5}
  hint           {"cameraName": "Static Image Camera",
                  "cameraSourceId": "static-image-camera",
                  "sourceDeviceId": "jetson-thor1"}
  violation      None            prior record untouched: yes    entry untouched: yes

arv-6c84191b7fe6     (AravisDiscovered, absent=false)
  resolved via   params.cameraId
  parameters     {"camera_id": "static-image-camera", "exposure": 100, "gain": 5}
  hint           {"cameraName": "AWS-DDA Static Image Camera",
                  "cameraSourceId": "arv-6c84191b7fe6",
                  "sourceDeviceId": "jetson-thor1"}
  violation      None            prior record untouched: yes    entry untouched: yes
```

- **Requirements 2.2, 2.3 on a PRESENT camera.** The `StaticImage` entry — still
  `params: {}`, now `absent: false` and carrying the pin metadata — resolves
  `'static-image-camera'` and `applyAravisCameraSelection` writes it into `camera_id`.
  `Required parameter 'camera_id' has no value` is structurally impossible for this
  entry: the resolution is non-null, so the key is always set to a non-empty string. This
  closes the §4 gap — the payload the replay ran over is no longer the absent one, it is
  the pinned, present, metadata-bearing entry a user would select today
- **bugfix.md 3.20 — both offered duplicates resolve the SAME id.** Resolved-id set
  across both present rows = `{'static-image-camera'}`, size **1**. Whichever of the two
  options the user picks binds the same device-side camera, and that camera is now
  actually serving the pinned frame
- **Requirement 3.4 — offered set unchanged.** `isAravisCompatibleCamera` over the
  present payload returns the identical seven ids in the identical order under the OLD and
  FIXED resolvers (`arv-6c84191b7fe6`, `arv-c9dd20f60ee1`, `arv-797b019251e9`,
  `arv-cf582dea7590`, `cfg-28183exv`, `cfg-o70qz7ci`, `static-image-camera`); both
  `Folder` rows stay out of both
- **Requirements 3.1, 3.3, purity.** All six non-static offered entries resolve
  byte-identically under both resolvers from `params.cameraId`; `gain`/`exposure` copying
  is unchanged (neither static row carries numeric ones, so the prior `5`/`100` survive
  untouched); neither the prior parameter record nor the registry entry is mutated

### 8.6 Aravis Fake camera unaffected by the pin (Req 3.16)

`arv-c9dd20f60ee1` after the pin: exactly **one** row matching `Fake_1` in the payload,
`absent: false`, `version: 1171`, `params.cameraId: 'Fake_1'`, resolving `'Fake_1'`
**from params** under both resolvers, with **no `staticImage` capability block** anywhere
on the entry (`'staticImage' in capabilities` → `False`) — and even if there were one, the
`type === 'StaticImage'` gate would skip the fallback. Applying the selection yields
`{"camera_id": "Fake_1"}` with no violation. Identical to the §5 baseline apart from the
`last_reported_at` bump; the pin did not touch it.

### 8.7 What §8 did NOT cover

The browser click-through itself (steps c/d rendered in a real browser) is still not
observed — no UI automation available, same constraint as §1. What ships in the verified
bundle plus what the live payload now contains determines the rendered outcome: the panel
reads `camera_id` from the same `applyAravisCameraSelection` result proven above, the
violation comes from the same `checkParameterValue` proven above, and the focus/scroll/flag
path was read out of the served bundle in §2 (iii). The task 1/2/3 suites assert all three
at the component level.

### 8.8 State left behind (intentional)

- **`jetson-thor1` is left with `dda-static-pin-test-640x480.png` PINNED**, per the user's
  choice. `pinned: true`, static camera present in both the device enumeration and the
  Portal registry, `latest` Pin_Request `01788889080505#55fe9832` = `applied`
- Portal-side records created by this run: one `PIN_REQUEST#01788889080505#55fe9832` item,
  one canonical S3 object `static-image-pins/jetson-thor1/01788889080505#55fe9832`, one
  `pin_static_image` audit event. The staging object was deleted by the pin route on
  success (and has a 1-day lifecycle rule as backstop)
- Nothing else was mutated: no unpin, no source file, no `tasks.md` checkbox, no commit,
  no build, no deploy

## What remains UNVERIFIED

One gap remains. Item 1 below was closed by §8; item 2 is the device track's component
build, which is the user's to run.

1. ~~**Pending the user's pin approval**~~ — **RESOLVED 17:37–17:43Z, see §8.** The user
   approved the pin and it was exercised end to end: upload-url → presigned PUT → pin
   submit (201, `01788889080505#55fe9832`) → `applied` in 692 ms →
   `deviceReported.present: true`, with the device confirming `pinned: true` and
   enumerating the static camera, the registry flipping both static rows to
   `absent: false`, and the binding replay resolving `'static-image-camera'` on the
   **present** payload with no required-parameter violation. The image is left pinned.
   Still outside this method's reach (unchanged constraint, §8.7): the browser
   click-through of steps (c)/(d) — the panel visibly rendering
   `camera_id = static-image-camera`, the violation visibly disappearing, and the Cameras
   tab visibly landing on the flagged static-image panel. All three ship in the verified
   bundle and are asserted by the task 1/2/3 suites
2. **Pending the user's Greengrass component build** (device track, tasks 6-10, ~100
   min + a deployment revision). `jetson-thor1` still runs the pre-fix LocalServer, and
   §8.4 confirms it behaviorally: with a pin in place it reports **two** present
   static-image rows. That is the expected and correct state for a frontend-only deploy —
   §8.5 shows both options bind the same id on the present payload, so the duplicate is
   cosmetic, not a dead end. Requirements 2.7-2.10 (one registration per pin state) and
   the one-shot shadow-key retirement (2.9, 2.11) remain the subject of task 10, which now
   has a concrete pinned baseline to diff against (§8.4: `count: 9` → expected `8`, two
   present static rows → expected one, `arv-6c84191b7fe6` → expected gone) and a timing
   caveat (re-read at least one full discovery interval after pinning, since the duplicate
   surfaces one report late)

<details>
<summary>Superseded — the original read-only framing of gap 1 (kept for the record)</summary>

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

</details>

## Cleanup

- **§§1-7 (read-only run):** nothing to clean — analysis artifacts confined to
  `/tmp/staticcam-verify/`; no portal state and no device state created or mutated
- **§8 (pinned run):** no cleanup performed **by design**. The user chose to leave the
  test image pinned, so `dda-static-pin-test-640x480.png` stays pinned on `jetson-thor1`,
  along with its `PIN_REQUEST#01788889080505#55fe9832` item, its canonical S3 object
  `static-image-pins/jetson-thor1/01788889080505#55fe9832`, and the `pin_static_image`
  audit event. The staging object was removed by the pin route itself on success. Local
  artifacts (test image, synthesized events, raw responses, binding replay) are in
  `/tmp/staticcam-verify/pin/`. To undo later:
  `DELETE /devices/jetson-thor1/cameras/static-image/pin`
