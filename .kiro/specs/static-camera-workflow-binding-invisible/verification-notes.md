# Static Camera Workflow Binding Invisible — Task 6 Hardware Verification Notes

Spec: `.kiro/specs/static-camera-workflow-binding-invisible` (bugfix).
Device: `jetson-thor1` (JetPack 7, `arm64_jp7`), account 164152369890, us-east-1.
Date: 2026-09-23, all times UTC.

## Build and deployment under test

| Item | Value |
|---|---|
| Source ref built | `wip/device-bugfixes-jp7-verify` (= local `integration/all-specs` HEAD `7e7dd2a`) |
| Build | DDA portal build system, dedicated JP7 server `Jp7-24.04-pro` (`i-0345420de4f9cd214`, m6g.4xlarge), job `0fb6e82a-6341-45b1-b0b0-eb2575175e6f`, ~25 min, SUCCEEDED |
| Published | `aws.edgeml.dda.LocalServer.arm64JP7` **1.0.44**; ECR `dda/flask-app:1.0.44`, `dda/react-webapp:1.0.44` |
| Deployment | `fbd816b5-07a6-46a4-85e2-d84c5843d71b` (`thor1-device-bugfixes-localserver-1.0.44`), COMPLETED; 20 components carried over, 4 component configurations preserved |
| Backend container | `awsedgemlddalocalserverarm64jp7-backend_tegra_gpu_enabled-1` |

Both fix legs were confirmed present in the running container before testing
(module paths are container-root, e.g. `/workflow_engine/runtime.py`,
`/workflow_engine/camera_binding.py` — not `/app/...`).

## (a) Bare `static-image-camera` binding registers as runnable

Workflow `ae783ac8-3cf2-4baf-b242-a3bb284776a9` **v2**
(`rfdetr-blue-plate-static-verify`), camera node `camera_1` bound by the
identifier itself — `cameraSourceId: "static-image-camera"`, no wrapping
Image_Source — delivered by portal deployment
`6a8d339f-41dd-480c-9e66-66f9c031276e` (`camera_bindings_delivered: true`).

- `GET /workflows/registrations` → `status: registered`, no `invalidReason`.
- **Zero** `missing camera source` lines in the backend log for the whole
  session. Pre-fix this line was emitted on every 5 s watch cycle.
- Executor log, per run:
  `Aravis feed for node camera_1 uses the device Image_Source configuration for camera 'static-image-camera'`
  then `Aravis frame feed planned for node camera_1: camera 'static-image-camera' (2001x2352)`.
- The compiled artifact set carries `bindingPoints[0].parameters.camera_id =
  static-image-camera` and the resolved assignment now carries both spellings
  (see (d)), which is what leg 2 fixed.

## (b) Repeated runs, real inference, backend healthy

10 engine-path executions between 03:43:41Z and 04:18:04Z, **all `completed`,
0 failed** (`workflow_executions` query, `started_at >= 1790135021`). Durations
0–2 s. Every run produced the same three detections:

| class | confidences |
|---|---|
| `blue_plate` ×3 | 0.9506 / 0.9403 / 0.9395 |

Byte-identical to the pre-fix workaround path (the wrapping Image_Source), i.e.
the fix changes how the camera is found, not what is inferred.

Container at the end of the session: `Status=running`, `Health=healthy`,
`RestartCount=0`, `OOMKilled=false`. Three backend restarts happened during the
session and all were deliberate (`docker restart`, for the cold-model spec's
reproduction); none was a crash.

## (c) Unpinned behaviour matches design.md Decision 3

Exercised through the real Device_Pin_API on the device loopback
(`127.0.0.1:5000`), with the pinned bytes backed up first
(sha256 `5fdb95e632208d6de64a6d2a1df310f0ecb989c038ade208cd0a951552052484`,
6 131 555 bytes, `blue-plate-3plates-8e87e714.jpg`) so the re-pin restored the
exact same image.

| Step | Time | Observation |
|---|---|---|
| `DELETE /static-image-camera/pin` | 03:59:39Z | `{"cameraId":"static-image-camera","pinned":false}` |
| registration after one watch cycle | 03:59:42Z (`registeredAt` 1790135982) | `status: invalid`, `invalidReason: "missing camera source static-image-camera"` — the reason names the camera |
| `POST /static-image-camera/pin` (multipart, same bytes) | 04:00:08Z | `pinned: true`, same width/height/size, new `pinnedAtEpochMs` |
| registration after one watch cycle | 04:00:13Z (`registeredAt` 1790136013) | back to `status: registered`, `invalidReason` cleared — **no redeploy** |
| trigger after re-pin | 04:01:43Z | execution `2918ff8d-0689-45b3-b5eb-8709c2c912ec` `completed`, same 3 detections |

Both transitions converged inside a single 5 s watch cycle.

## (d) The `cfg-my6j3zx1` wrapping-Image_Source path still resolves (Req 3.4)

Verified **at the resolver, on the device, against the live inventory** — a
script run inside the backend container that reads the real `image_source` /
`image_source_configuration` rows (SQLite `mode=ro`), the real pin store
`status()`, and the real `compiled_pipeline.json`, then calls the production
`build_inventory` → `resolve_bindings` → `plan_aravis_feeds`:

```
inventory ids: ['cfg-28183exv', 'cfg-920nufdy', 'cfg-iebllnt4', 'cfg-my6j3zx1',
                'cfg-o70qz7ci', 'cfg-pgc367hy', 'static-image-camera']

cameraSourceId=static-image-camera   entry params {}                              -> resolved
    assignment params {"cameraId": "static-image-camera", "camera_id": "static-image-camera"}
    feed plan ('camera_1', 'static-image-camera', {})

cameraSourceId=cfg-my6j3zx1          entry params {"cameraId":"static-image-camera"} -> resolved
    assignment params {"cameraId": "static-image-camera", "camera_id": "static-image-camera"}
    feed plan ('camera_1', 'static-image-camera', {})
```

The two assignments are identical, which is exactly what leg 2 set out to
achieve: a static assignment is shape-indistinguishable from a configured one,
so every downstream reader works unchanged. The virtual entry still carries
`params: {}` with its identity under `capabilities.staticImage` — the shipped
contract pinned by Reqs 3.5/3.17 is untouched.

**Gap, stated plainly.** An end-to-end *run* through the `cfg-my6j3zx1` binding
was **not** performed on the fixed build. Changing a delivered binding requires
the Portal to write `desired.bindings` into the `dda-camera-bindings` shadow, and
during this session the device's cloud→device shadow sync was not delivering:
the cloud document reached version 51 while the device's local copy stayed at
version 49 (read back over IPC). Writing the local shadow directly does not help,
because `CameraBindingStore` only invalidates its cache on the cloud delta
subscription — with the local document changed to a deliberately absent
`cfg-does-not-exist`, the registration stayed `registered`, proving the cache was
still serving the older value. The end-to-end wrapping-binding path was exercised
on this same device pre-fix (it is the RF-DETR workaround, 15 runs), and the
resolver evidence above shows the fixed build produces an identical feed plan for
it, but the post-fix end-to-end run remains unverified.

## Incidental observation: transient `bindings unavailable`

Twice during the session every registration with binding points flipped to
`invalid: bindings unavailable` — 04:04:43Z→~04:07:19Z (recovered on its own)
and 04:11:40Z→04:16:50Z (cleared by the deliberate backend restart). Cause was
the device's shadow read over IPC failing
(`IoTShadowAccessor: Exception occurred:` with an empty exception string, i.e. an
`awsiot` model error), not anything in this fix — neither changed file touches
the shadow or IPC path, and the whole log before 04:04:43Z contains zero
`bindings unavailable` lines. Both episodes coincided with out-of-band
`UpdateThingShadow` calls made while attempting (d), so they are most likely
shadow-manager sync churn on a device whose cloud→device sync was already lagging.
The design's "never cache a failure" rule did its job: no intervention was needed
the first time, and no workflow run failed as a result. Worth a separate look if
it recurs without out-of-band shadow writes.

## Test-environment notes

- Device pin API and engine API are on the host loopback at `127.0.0.1:5000`
  (the backend container uses host networking, so `docker ps` shows no published
  ports for it).
- Deployed runs were triggered with
  `POST /workflows/registrations/{registrationId}/trigger`. Publishing the
  workflow's local `rfdetr/invoke` pubsub topic from a second IPC client fails
  with `UnauthorizedError` — LocalServer's recipe authorizes the subscription,
  not an outside publisher.
- Detections live at
  `/aws_dda/captures/{workflowId}/{executionId}/*.detections.json`, shaped
  `{"detections": {"0": {...}, "1": {...}}}` — an index-keyed map, not a list.
