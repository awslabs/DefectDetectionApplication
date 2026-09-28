# Static Camera Video Loop: Verification Notes

All times are UTC, 2026-09-26. The tree under test is `spec/static-camera-video-loop`: `integration/all-specs` `f11f172` plus this change, uncommitted. "Base" means a detached worktree of `f11f172`, run the same way.

## Task 12: local checkpoint

**Device suites** ran in `dda-flask-test:vl-arm64cpu`, one suite per process, with `HYPOTHESIS_PROFILE=ci` for the three camera suites and `fast` for the others.

| Suite | Result |
|---|---|
| `static_video_camera` | 127 passed |
| `static_image_camera` | 125 passed, 1 failed (pre-existing, see below) |
| `camera_sync` | 94 passed |
| `camera_discovery` | 57 passed |
| `camera_lifecycle` | 9 passed |
| `camera_shadow_sync` | 26 passed |
| `workflow_engine` | 1610 passed, 3 skipped |
| `api-endpoints` | 140 passed, 3 failed (pre-existing) |
| `preservation` | 10 passed, 3 skipped (runtime-deferred checks) |
| `security` | 233 passed, 15 skipped (see task 13.1) |

**Portal backend, full suite.** It ran in `dda-portal-test:py312`, in 16 shards, with `--continue-on-collection-errors`. The same run was made on base.

| | Passed | Failed | Errors | Skipped |
|---|---|---|---|---|
| This change | 5484 | 43 | 43 | 10 |
| Base | 5398 | 61 | 43 | 10 |

- The test IDs present only in this run are exactly the 68 tests in the four new files: `test_pin_video_routes_examples` (27), `test_shadow_document_size_limit` (36), `test_video_loop_parity_properties` (4) and `test_pin_video_submission_properties` (1). No test ID is present only on base.
- There are two new failures: `test_deployment_preflight_preservation.py::TestSourceTreeUntouched::{test_no_tracked_file_under_src_is_modified, test_no_recipe_or_dockerfile_is_modified}`. Both fail because tracked `src/` files are uncommitted, and they list only this change's `src/backend` files. On base they skip ("not a git work tree"). They pass once the change is committed.
- 20 failures occur only on base: the `test_git_sync_runner` tests. `git` cannot use the base worktree's `.git` file inside the container. On this change they pass.
- The other 84 failures and errors are identical in both runs. They include four collection errors caused by modules missing from the test image (`numpy`, `fastapi`, and a `shared_utils` import).
- **With OpenCV.** The video and quota test files and the two modified camera test files also ran in `dda-portal-test:py312-vl`, which has `cv2` 4.11.0 and `numpy` 1.26.4, the video layer's pins: 75 passed, 0 skipped. In the py312 image, `test_real_child_decodes_a_real_clip` and `test_real_clip_pins_through_the_child_runner` skip because OpenCV is absent; here they ran against real clips through the child-process probe.
- **Deviation.** An earlier run (tag ck12) used no `--continue-on-collection-errors`. In both of its trees, four of the 16 shards stopped at a collection error and ran nothing, so its totals were incomplete. The run above replaces it.

**Infra.** `npx tsc -p .` then `npx jest`: 24 suites and 267 tests passed. The IAM synth gate (`test_preservation_iam_cdk_synth.py`) was run on the host, because inside a container it skips the synth: 11 passed. The additions it accepts are the ones recorded in `iam_post_fix_approved_additions.json`:
- `servicequotas:GetServiceQuota` on `iotcore/L-A295A064` for the Deployments role and for `DDAPortalAccessRole`;
- `lambda:InvokeFunction` from `dda-portal-camera-video-pin` to itself.

**Frontend.** Full vitest: 217 files and 2207 tests passed. `tsc` and `vite build` (built into a scratch `outDir`) succeeded.

**Mutation checks.** Hand-made mutants of the new logic were all caught by the new tests: device 4 of 4, Portal 5 of 5, frontend 5 of 5.

## Task 13.1: preservation and security suites

- `test/backend-test/security` in the flask-app container: 233 passed, 15 skipped. Every skip is environmental:
  - `torch` or `jwt` is not installed in the image;
  - the AWS CLI is absent;
  - `cdk synth` does not run in a container (the host gate above covers it);
  - `cdk.out` has been moved aside;
  - the vendored `edgemlsdk` duplicates are gitignored.
- `test/backend-test/preservation`: 10 passed and 3 skipped. The skipped checks are deferred to the runtime and build gates.
- The pre-build guard pair on the host (`test_preservation_out_of_scope_guard.py` and `test_preservation_secrets_out_of_scope_guard.py`, run with `--noconftest`): 4 passed, 3 skipped.
- `iam_out_of_scope_baseline.json` changes only in the two intended entries plus the note:
  - `src/backend/app.py` `8ba8db65` → `74340eaa`;
  - `src/backend/utils/camera_manager.py` `d932f7e4` → `3a2b05a8`.
  - The `sha256sum` of both files matches the new values.
- `git status` shows no change to any recipe, Dockerfile, `docker-compose.yaml`, `src/backend/requirements.txt`, `station_install/setup_station.sh` or `gdk-config.json`.
- `edge-cv-portal/infrastructure/cdk.out` is moved aside, to `cdk.out.bak-20260926T145805Z`.

## Task 13.2: platform images

**How the images were built.** Each test image is the platform's LocalServer `flask-app` image plus one pip layer: pytest 8.3.5, hypothesis 6.131.0, sarge, testfixtures and httpx. For the four arm64 images, the layer-prefix comparison against the base was verified. Each image ran the suites with its own app interpreter.

**The arm64 images** ran on this host. **The two x86 images** ran on the x86 build server `i-0ae8ec99335683610`: the tree went over as an S3 tarball, the images were built there and removed afterwards, and the S3 prefix was deleted.

| Image (base) | Python | ffmpeg | `cv2` Video I/O |
|---|---|---|---|
| arm64 CPU (`dda/flask-app:arm64-1.1.0`) | 3.11 | 4.4.2 | 4.11.0, FFMPEG YES (avcodec 59.37.100, avformat 59.27.100) |
| JP5 (`dda/flask-app:arm64JP5-1.0.45`) | 3.11 | 4.2.7 | 4.11.0, FFMPEG YES (avcodec 59.37.100, avformat 59.27.100) |
| JP6 (`dda/flask-app:arm64JP6-1.0.69`) | 3.10 | 4.4.2 | 4.11.0, FFMPEG YES (avcodec 59.37.100, avformat 59.27.100) |
| JP7 (`dda/flask-app:arm64JP7-1.0.45`) | 3.11 | 6.1.1 | 4.11.0, FFMPEG YES (avcodec 59.37.100, avformat 59.27.100) |
| amd64 (`flask-app:latest` on the x86 server) | 3.11 | 4.4.2 | 4.11.0, FFMPEG YES (avcodec 59.37.100, avformat 59.27.100), GStreamer NO |
| amd64Nvidia (`dda/flask-app:amd64Nvidia-1.0.0`) | 3.11 | 4.4.2 | 4.11.0, FFMPEG YES (avcodec 59.37.100, avformat 59.27.100), GStreamer NO |

**Results** (`HYPOTHESIS_PROFILE=ci`):

| Image | `static_video_camera` | `static_image_camera` | `camera_sync` |
|---|---|---|---|
| arm64 CPU | 127 passed | 125 passed, 1 failed* | 94 passed |
| JP5 | 125 passed, 2 skipped† | 125 passed, 1 failed* | 94 passed |
| JP6 | 127 passed | 125 passed, 1 failed* | 94 passed |
| JP7 | 127 passed | 125 passed, 1 failed* | 94 passed |
| amd64 | 127 passed | 125 passed, 1 failed* | 94 passed‡ |
| amd64Nvidia | 127 passed | 125 passed, 1 failed* | 94 passed‡ |

\* This is `test_camera_manager_short_circuit.py::test_static_grab_touches_no_camera_machinery`, which fails with `'_thread.RLock' object has no attribute 'locked'`. It fails identically on base.

† **JP5 skips.** Two tests, `test_av1_upload_names_the_codec` and `test_worker_reports_the_store_reason_for_an_undecodable_video`, need an AV1 clip, and JP5's ffmpeg 4.2.7 has no AV1 encoder. To close that gap, the AV1 clip made in the arm64 CPU image was probed in the JP5 image with `video_loop.probe_video`. It was rejected with: "The video could not be decoded (codec: AV1). Supported video codecs: H.264, H.265/HEVC, MPEG-4 Part 2, Motion JPEG, VP8, VP9."

‡ **x86 `camera_sync` rerun.** The first x86 run reported `camera_sync` as 93 passed, 1 failed. The tarball held only `src/backend` and `test/backend-test`, and `test_property_portal_change_round_trip` loads `edge-cv-portal/backend/functions/camera_sync.py` by path, so it failed with `FileNotFoundError`. The suite was rerun on both x86 images with that directory added and the same tree otherwise: 94 passed on each.

**Timing.** `static_video_camera` takes 32–38 s per image, and `camera_sync` 35–37 s.

## Pre-existing failures, identical on base

- **Device:**
  - `static_image_camera/test_camera_manager_short_circuit.py::test_static_grab_touches_no_camera_machinery`;
  - in `api-endpoints`: `test_connect_camera_endpoint`, `test_frame_requires_viewer_id_query_param` and `test_settings_apply_error_returns_422_naming_control`.
- **Portal:** the 84 failures and errors shared by both runs, including the `TestPortalAdminGate` cases in `test_user_admin_change_role.py` and `test_user_admin_delete.py`.

## Task 14: build, deploy and on-device verification

Date: 2026-09-26/27, all times UTC.

**Source.** `wip/static-camera-video-loop-verify` = `da1cedf` is a snapshot commit of the 75 changed files on top of `f11f172`. It was made with a temporary index, so the spec branch and the working tree are untouched. `git hash-object` equals the snapshot blob for all 75 files. It reached the JP6 and JP7 fleet servers' clones as a git bundle over S3, as a local branch only; nothing was pushed to GitHub. The JP5 build used this tree directly; the amd64 build used a `git archive` of the snapshot.

### 14.1 Pre-build checks

- No build was running, on this host or on the fleet servers.
- `cdk.out` had been moved aside.
- No Portal stack was in progress.
- The host guard pair passed, and none of the preservation-tracked files had changed.
- The in-build gate body (the auth tests and every security audit gate) was rehearsed in the JP5 test image first. It passed.

### 14.2 Builds and published versions

| Target | Where | Result | Published |
|---|---|---|---|
| JP5 | this host, `TARGETS=5 ./run_jp_builds.sh` then `SKIP_BUILD=1 ./portal-build.sh aarch64 5` | build 20:46–21:58; in-build gates passed; ECR path (6.1 GB zip) | `aws.edgeml.dda.LocalServer.arm64JP5` **1.0.47** |
| JP6 | fleet job `387853b5`, JP6 Build Server `srv-aac90870` | "Source synced to da1cedf"; 21:48–23:17; gates passed (python 3.10) | `arm64JP6` **1.0.70** |
| JP7 | fleet job `a76ce380`, `Jp7-24.04-pro` `srv-af3e3e08` | "Source synced to da1cedf"; 21:48–01:10 (vLLM rebuilt from source); gates passed | `arm64JP7` **1.0.47** |
| amd64 | x86 build server `i-0ae8ec99335683610`, `./portal-build.sh x86_64` in its own directory | 890 s; gates passed; GDK path (1.3 GB zip) | `amd64` **1.0.42** |
| amd64Nvidia | — | skipped: there is no test device (as agreed) | — |

- Each published recipe was compared with the previous version's. The dependencies and platform are the same; only the artifact tags or URIs moved to the new version.
- The build ran `portal-build.sh` over the tracked `recipe.yaml` and `gdk-config.json`. Both were restored afterwards and are identical to `HEAD`.

### 14.3 Portal deploy

- **Before the deploy.** `cdk diff`, written to a scratch output directory, showed these changes:
  - the new `dda-portal-camera-video-pin` function, the `VideoLayer`, and the four routes;
  - the two approved IAM statements plus the API Gateway invoke permissions;
  - the usual layer and function churn.
  - The video function totals 212 MB unzipped, against Lambda's 250 MB limit.
- **The deploy.** `deploy-infrastructure.sh` ran 22:05–22:20 and `deploy-frontend.sh` 22:21–22:28, with 0 failures. `cdk.out` was moved aside again afterwards.
- **Checks.** The four new routes answer 401 unauthenticated, while a nonexistent route answers 403.
  - The function is deployed as designed: python3.12, 120 s timeout, 2048 MB memory, 1024 MB ephemeral storage, `SharedLayer:87` and `VideoLayer:1`, `VIDEO_VALIDATION_FUNCTION` set, async retries 0, maximum event age 300 s.
  - The CloudFront bundle `index-3Mf2JxyE.js` carries the video panel and the shortcut.
- **Live validation, with no device involved** (on `jetson-thor1`, before its upgrade):
  - An AV1 clip returned 202, then was rejected in 1.2 s: "The video could not be decoded (codec: AV1)…".
  - A PNG named `.mp4` was rejected as "not a supported video".
  - A 101 MiB file got a synchronous 400 naming the 100 MB limit.
  - None of these created a pin request, and the staging prefix was left empty.
- **The temporary Portal user.** It had `custom:role` DataScientist while the two fleet builds were submitted (`builds:submit` is checked at global scope), then Viewer, plus a UseCaseAdmin row on use case `645504ce`.

### 14.4 Devices

**Deployments.** Each one revises the device's current thing deployment and changes only the LocalServer version. For the MIC-730 and the Dell, the components, configuration merges, policies and tags are copied verbatim.

| Device | Before | Deployment | Result |
|---|---|---|---|
| `mic730jp513-ryvanlabhome` (JP5) | 1.0.46 | `a0fa9ae3` → 1.0.47 | COMPLETED; containers healthy (LogManager logs) |
| `ryanhomelabdellworkstation` (amd64) | 1.0.41 | `613840fa` → 1.0.42 | COMPLETED; containers healthy (LogManager logs) |
| `ryanorinagxdevkithomelabjp622` (JP6) | 1.0.69 | `c68aee9d` → 1.0.70 | COMPLETED; containers recreated from `arm64JP6-1.0.70` at 23:24Z |
| `jetson-thor1` (JP7) | 1.0.46 | `97231d5f` → 1.0.47 (07:13Z) | COMPLETED |

**Temporary verification workflows.** A later round of deployments added the two single-camera verification workflows and afterwards restored each device's component set verbatim:

- thor1: `f9d03fbd`, then `0f41e91e`.
- Orin: `78e08571`, then `009918b0`.
- MIC-730: `f66da2c5`, then `ec9c9f81`.

These were direct thing-deployment revisions. The Orin's first round used the Portal (`38168430`, then `a732398b`). Every one COMPLETED.

#### Orin AGX (JP6, `ryanorinagxdevkithomelabjp622`)

- **Code.** The container's 12 changed backend files have the same sha256 as the snapshot.
- **Quota and report cap.** The effective ShadowManager configuration has no `shadowDocumentSizeLimitBytes`, so ShadowManager enforces its 8192-byte default. The account quota `L-A295A064` is 8192 (the increase request to 16384 is still CASE_OPENED). So the agent's cap is 8192 − 3584 = 4608 bytes. The Portal's own deployment (below) submitted a ShadowManager merge carrying only `synchronize`, byte-identical to before.
- **Portal pins.**
  - The image `vl-test-card-720p.jpg` was applied 1.05 s after the request.
  - Video A (`vl-scene-a-720p30-h264-12s.mp4`, H.264 1280×720, 30 fps, 12 s): the validation was accepted 1.5 s after the 202. The Portal-validated metadata matched the device-reported metadata, and the device applied the pin request 1.0 s after it was created.
- **Enumeration.** `/cameras` lists `static-image-camera` and `static-video-camera` as separate cameras. The Portal registry shows two synced rows, `StaticImage` and `StaticVideo`, with their capabilities.
- **Portal UI** (headless Chrome, logged in as the temporary user):
  - The Cameras tab shows "Static video camera" with the focus flag, "Video pin applied", "Device reports a pinned video", and the full metadata grid.
  - In the Workflow Builder, the camera picker for this device lists "Static Image Camera (StaticImage)" and "Static Video Camera (StaticVideo)" separately. "Pin a static test image…" and "Pin a test video…" are both enabled once a device is chosen.
- **Preview (Image_Source preview path).** 8 previews 1 s apart gave 8 distinct images, p50 135 ms (the first took 2.4 s: player open). The image camera gave 4 identical images, p50 80 ms.
- **Grab latency** (`StaticVideoStore.get_frame` inside the container, 60 grabs every 100 ms): p50 7.6 ms, p95 9.6 ms, first grab 64.7 ms. Back-to-back grabs within one frame period take ≈0 ms. The image camera takes 0.1 ms.
- **Workflows.** The Portal validator rejects two `aravis_camera_source` nodes in one workflow (`V7_COEXISTENCE_CONFLICT`, a pre-existing platform rule; see Deviations). The check therefore used one workflow per camera: `vl-verify-video-jp6` and `vl-verify-image-jp6`, each one camera node feeding a capture node. They were packaged for `arm64_jp6` only, and each depends only on `LocalServer.arm64JP6 >=1.0.0`. They were deployed through the Portal's `POST /deployments` (deployment `38168430`).
  - The video workflow ran 6 times, 2.5 s apart: all completed in 0.59 s, with 6 distinct captures.
  - The image workflow ran 3 times with identical captures.
  - Both workflows run concurrently (4 + 4 runs started in the same seconds): the video captures are distinct and the image captures identical.
- **Replace.** Video B (`vl-scene-b-480p25-hevc-6s.mp4`, HEVC 640×480, 25 fps) was accepted 1.9 s after the 202 and applied. The next workflow captures were 640×480; the ones before were 1280×720.
- **Remove video.** Applied 215 ms after the request. The Portal shows the device reporting the camera absent. `/cameras` drops `static-video-camera`.
  - The image camera is unaffected: same metadata, and an identical capture hash.
  - The video workflow now fails naming the cause: "Static video camera 'static-video-camera': no usable pinned video is available…".
- **Remove image.** Applied 300 ms after the request; both virtual cameras are gone from `/cameras`.
- **Device API.**
  - A multipart video pin returned 200 in 147 ms, and the camera is enumerated.
  - A non-video file returned 400 "not a supported video".
  - `DELETE` unpins the video.
- **Health.** Backend and frontend are `running`/`healthy`, with RestartCount 0 and no OOM kill. The containers were recreated only by the deployments (23:24, 23:33 and 00:00).
- **Left as found.**
  - The original device-API pin `zidane.jpg` was re-pinned (same bytes, sha256 `16d73869…`).
  - No video is pinned.
  - The verification workflows were removed from the deployment (Portal deployment `a732398b`, COMPLETED).

#### thor1 (JP7, `jetson-thor1`)

- **Code and cap.**
  - The container's 12 changed backend files match the snapshot.
  - ShadowManager has no `shadowDocumentSizeLimitBytes`, so the cap is 4608 bytes, as on the Orin.
  - The reported camera document is 4070 bytes for 11 cameras, both virtual cameras included, with no capability truncation. The whole shadow state with both pin slots is 6023 bytes.
- **Portal pins.**
  - The image `vl-test-card-720p.jpg` was applied 0.6 s after the request.
  - Video A: the validation was accepted 1.3 s after the 202, and the device applied the request 0.8 s after it was created.
  - The Portal UI shows the same as on the Orin: the video panel reports "Video pin applied", "Device reports a pinned video" and the metadata. The picker lists "Static Image Camera (StaticImage)" and "Static Video Camera (StaticVideo)", and both shortcuts are enabled.
- **Grab latency.** Paced grabs: p50 4.2 ms, p95 9.5 ms, first grab 44.2 ms, 60 of 60 frames distinct. The image camera takes 0.0–0.1 ms.
- **Workflows.** `vl-verify-video-jp7` and `vl-verify-image-jp7`:
  - 6 video runs: 6 distinct captures, 0.53 s warm.
  - 3 image runs: identical captures.
  - 4 + 4 concurrent runs: the video captures are distinct and the image captures identical.
- **Replace.** Video B (HEVC 640×480) was accepted 1.8 s after the 202 and applied. The next captures are 640×480.
- **Remove video.** Applied: the device reports the camera absent 0.2 s after the request.
  - `/cameras` drops the video camera.
  - The image workflow's capture hash is unchanged.
  - The video workflow fails naming the cause.
- **Remove image.** Absent 0.23 s after the request; both virtual cameras are gone.
- **Device API.** A video pin took 97 ms. A non-video file returned 400, and `DELETE` unpins the video.
- **Backend restarts.** Each deployment was followed by one clean backend exit ("Local server shutdown complete; exiting") about 1 s after the vLLM model reported READY: at 07:21, 17:55 and 18:43. This is the pattern already recorded for 1.0.45 → 1.0.46 in `run-detection-visibility`.
  - After each restart the Triton models showed UNKNOWN until they were started through the feature-configurations start route. That was done every time, and 9 of 9 models are READY.

#### MIC-730 (JP5, `mic730jp513-ryvanlabhome`, SSH port 9994)

- **Code.** The container's 12 files match the snapshot. The cap is 4608 bytes (no ShadowManager limit set), and the reported document is 1897 bytes.
- **Pinning.** The device has no Portal use case, so pinning went through the device API:
  - the image took 119 ms;
  - video A (4.8 MB) took 519 ms;
  - the replace with video B took 798 ms.
  - `/cameras` lists both virtual cameras.
- **Grab latency.** Paced grabs: p50 25.7 ms, p95 31.5 ms, first grab 145.6 ms, 60 of 60 frames distinct. The image camera takes 0.3 ms.
- **Workflows.** `vl-verify-video-jp5` and `vl-verify-image-jp5`:
  - 6 video runs: all completed and all different (0.67 s warm; the first, cold, took 4.3 s).
  - 3 image runs: identical captures.
  - 4 + 4 concurrent runs: the video captures are distinct and the image captures identical.
  - After the replace, the captures are 640×480.
- **Remove video.** The image capture hash is unchanged, and the video workflow fails naming the cause.
- **Remove image.** Both virtual cameras are gone.

#### Dell (amd64, `ryanhomelabdellworkstation`, SSH port 9993, host `edgml-workstation`)

- **Code and cap.**
  - LocalServer 1.0.42 had been up and healthy for 22 hours when checked.
  - The container's 12 files match the snapshot.
  - No ShadowManager limit is set, so the cap is 4608 bytes. The reported document with both virtual cameras is 1898 bytes.
- **Pinning.** The device has no Portal use case, so pinning went through the device API:
  - the image took 100 ms;
  - video A took 383 ms;
  - the replace with video B took 465 ms;
  - a non-video file returned 400 "not a supported video".
  - `/cameras` lists both virtual cameras.
- **Grab latency.** Paced grabs: p50 4.4 ms, p95 9.4 ms, first grab 128.5 ms, 60 of 60 frames distinct. The image camera takes 0.1 ms.
- **Persistence.** Adding the workflows recreated the containers at 20:00:14. Both pins came back with their original `pinnedAtEpochMs` (Requirement 7).
- **Workflows.** `vl-verify-video-amd64` and `vl-verify-image-amd64` (x86_64 packages, depending on `LocalServer.amd64 >=1.0.0`):
  - 6 video runs: 6 distinct captures, 0.58 s warm and 1.65 s cold.
  - 3 image runs: identical captures.
  - 4 + 4 concurrent runs: the video captures are distinct and the image captures identical.
  - After the replace, the captures are 640×480.
- **Remove video.** The image capture hash is unchanged, and the video workflow fails naming the cause.
- **Remove image.** Both virtual cameras are gone.
- **Soak.** 20:02:55–20:32:55Z, with video B pinned:
  - 60 of 60 video runs completed (38 distinct) and 30 of 30 image runs;
  - 0 health failures;
  - the same container throughout, with no restart and no OOM kill;
  - memory 1.67 → 1.72 GiB, flat after the first 5 minutes.
- **Left as found.** Nothing is pinned, as before, and 4 of 4 models are READY.
  - The deployment is restored verbatim (`d9b3a1e7` added the workflows, `bd32734c` removed them).
  - The workflow directories and the `/tmp` files are removed.
  - The two amd64 components and their S3 packages are deleted.
- **Finding: inventory lags after a device-API replace.**
  - What happens:
    - A device-API pin or unpin changes enumeration, so the next discovery pass reports it.
    - A replace does not change enumeration, and the pin endpoints do not request a report.
    - So the camera-registry shadow kept video A's metadata from the replace at 20:02:49 until the removal was reported at 20:35:25Z.
  - What stays correct:
    - Frames and captures switched to video B immediately.
    - Portal-initiated replaces are reported right away, through the agent's portal-change path.
  - The image camera's device API behaves the same way (existing behavior). See Deviations.

#### 30-minute soak (thor1, Orin, MIC-730 in parallel 18:05–18:35Z; Dell 20:02–20:32Z)

**Setup.** Both virtual cameras were pinned, and both verification workflows were deployed.

- Every 30 s the soak ran the video workflow; every 60 s it ran the image workflow.
- It checked `/health` every round and recorded the backend container's state every 5 minutes.

| Device | Video pinned | Video runs | Image runs | Health failures | Container | Backend memory start → end |
|---|---|---|---|---|---|---|
| thor1 | B (HEVC, 25 fps, 6 s) | 60 of 60 completed | 30 of 30 | 0 | same container, no restart, healthy, no OOM | 3.30 → 3.44 GiB |
| Orin | A (H.264, 30 fps, 12 s) | 60 of 60 completed, 55 distinct | 30 of 30 | 0 | same container, no restart, healthy, no OOM | 16.42 → 16.91 GiB (flat over the last 10 min) |
| MIC-730 | B (HEVC, 25 fps, 6 s) | 60 of 60 completed, 29 distinct | 30 of 30 | 0 | same container, no restart, healthy, no OOM | 3.58 → 3.59 GiB |
| Dell | B (HEVC, 25 fps, 6 s) | 60 of 60 completed, 38 distinct | 30 of 30 | 0 | same container, no restart, healthy, no OOM | 1.67 → 1.72 GiB |

- **Image runs.** Every image run on every device captured the identical image.
- **thor1's two distinct video captures.** The 30 s interval is an exact multiple of video B's 6 s loop. All 60 runs therefore hit the same loop position (frame 111; once 110), which is correct real-time looping.
  - Six more runs about 1.8 s apart gave 6 distinct captures.

#### Left as found

- **Orin.** The original device-API pin `zidane.jpg` is re-pinned, with the same bytes (sha256 `16d73869…`). No video is pinned.
- **thor1.** Its original device-API pin `ppe-hse-factory.png` is re-pinned, with the same bytes (sha256 `24783d3d…`). No video is pinned, and 9 of 9 models are READY.
- **MIC-730 and Dell.** Nothing is pinned on either, as before.
- **Verification workflows.**
  - They are removed from all deployments.
  - Their leftover `/aws_dda/workflows/<id>` and `/aws_dda/captures/<id>` directories are deleted on each device.
  - The 8 Portal records are deleted.
  - All 8 Greengrass component versions (JP5, JP6, JP7 and amd64) are deleted, together with their packages in `s3://ryvan-cookies/workflows/components/`.
- **Scripts.** The device-side test scripts, media and logs in `/tmp` are removed.
- **The Orin's stale key.** The temporary preview Image_Source key `cfg-w2whc2as` was removed from the shadow by writing a `null` for it, and the Portal registry row went away.
- **Pin objects.** No pin objects remain under `static-image-pins/`.

### 14.6 Inventory report on device-API pin changes (the Requirement 4.6 fix)

Date: 2026-09-27/28, all times UTC. Status: verified on all four devices, each with a 30-minute soak.

**Change.** `endpoints/static_video_camera.py` calls `camera_sync.hooks.notify_image_source_changed()` after each successful pin, replace, pin by reference and unpin. The call never raises. Rejected requests request no report. The Static_Image_Camera endpoint is unchanged (Requirement 6.3).

**Suites** (arm64 CPU test image, each suite run on its own as in task 13.2):

| Suite | Result |
|---|---|
| `static_video_camera` | 130 passed, including 3 new endpoint tests |
| `static_image_camera` | 125 passed, 1 failed (the pre-existing `RLock.locked()` failure) |
| `camera_sync` | 94 passed |
| `security` | 233 passed, 15 skipped |
| Guard pair (host) | 4 passed, 3 skipped |
| IAM out-of-scope guard (host) | 2 passed; no baseline change, since the endpoint file is not pinned |

Running `static_image_camera` and `camera_sync` in one pytest process gives 6 more failures in `test_server_setup_isolation.py`, caused by module state shared across the two directories. Each suite passes on its own, and last cycle they were also run separately.

**Source.** `wip/static-camera-video-loop-verify2` = `a0ade54`, a child of `da1cedf`. It was made with a temporary index; its 3,283 blobs under `src`, `test`, `edge-cv-portal`, `scripts` and both spec folders equal the working tree. It reached the fleet clones as a git bundle, and the amd64 build used a `git archive` of it. Nothing was pushed.

**Builds**, run strictly one after another (builds.md):

| Target | Where | Result | Published |
|---|---|---|---|
| JP5 | this host | build 21:40–22:07, in-build gates passed; publish 22:07–22:15 | `arm64JP5` **1.0.48** |
| amd64 | x86 build server, own directory | 22:15–22:20 (289 s), gates passed | `amd64` **1.0.43** |
| JP6 | fleet job `230e4a64`, JP6 Build Server | "Source synced to a0ade54"; 22:21–22:38 (1,051 s) | `arm64JP6` **1.0.71** |
| JP7 | fleet job `32e9a984`, `Jp7-24.04-pro` | "Source synced to a0ade54"; 22:39–23:00 (1,269 s) | `arm64JP7` **1.0.48** |

Each fleet job was submitted by a temporary Cognito principal (user, app client and global role row), which was removed right after submission. None remain.

**Devices.** Each deployment revision changes only the LocalServer version, as in `make_revision.py`:

- MIC-730 `9aabe315`, 1.0.47 → 1.0.48;
- Dell `cf20f5c8`, 1.0.42 → 1.0.43;
- Orin `2fd76b9e`, 1.0.70 → 1.0.71;
- thor1 `79386f9f`, 1.0.47 → 1.0.48, deployed once its login was available. Its models have to be restarted by hand after a backend restart.

Each device ran the three checks through its device API, then a 30-minute soak. The soak replaced the video every 60 s, alternating two clips (640×360 at 30 fps and 320×240 at 25 fps), and polled `/health` every 30 s. "Latency" is the time from the API call until the camera-registry shadow's `static-video-camera` entry showed the change.

| Device | Pin | Replace | Unpin | Soak (30 replaces) | Health | Container |
|---|---|---|---|---|---|---|
| Dell, amd64 1.0.43 | 2.4 s / 2.3 s | 4.6 s / 4.3 s | 5.2 s / 5.2 s | 30 of 30 reached the shadow; median 2.3 s, max 5.5 s | 0 of 60 failed | same container, 0 restarts, no OOM; 1.47 GiB flat |
| MIC-730, JP5 1.0.48 | 2.3 s | 5.0 s | 5.0 s | 30 of 30; median 2.1 s, max 5.1 s | 0 of 60 failed | same container, 0 restarts, no OOM; 3.49 → 3.51 GiB |
| Orin, JP6 1.0.71 | 1.8 s | 4.8 s | 4.7 s | 30 of 30; median 2.1 s, max 4.8 s | 0 of 60 failed | same container, 0 restarts, no OOM; 17.23 → 17.50 GiB |
| thor1, JP7 1.0.48 | 1.8 s | 4.8 s | 5.0 s | 30 of 30; median 2.0 s, max 5.1 s | 0 of 60 failed | same container, restart count unchanged at 1 (see below), no OOM; 15.78 → 16.07 GiB |

- The Dell ran the three checks twice: once alone, and once at the start of its soak.
- Before the fix, a device-API replace on the Dell stayed stale for 33 minutes.
- The ceiling of about 5 s is the agent's 5-second debounce: a change made just after a report waits for the window to close.
- **The Orin's Basler.** The user attached a Basler acA4600-10uc (`Basler-26760165225D-23405149`) to the Orin during its soak. The backend enumerated it and the registry reported it (23:30:17Z) while the soak ran, with no health failure and no restart.
- **thor1's models.** 9 of 9 were READY before the deployment. After the restart they were not READY. They were started one by one through `/feature-configurations/models/<name>/start`, and all 9 were READY at 23:29:12Z, before the checks began. Still 9 of 9 READY after the soak.
- **thor1's restart count.** The 1.0.48 container was created at 23:21:42Z.
  - At 23:24:01Z the backend shut itself down: it unloaded the vLLM model and logged "Local server shutdown complete; exiting.". Docker restarted it at 23:24:02Z (restart policy `always`), before the checks.
  - It was not a crash. No crash signature appears in the log before that point.
  - The 1.0.47 container did the same after its deployment (created 18:41:12Z, restarted 18:43:31Z).
  - The count stayed at 1 through the soak.
- **Left as found.**
  - The video is unpinned on all four devices, as before, and the test clips are removed from `/tmp`.
  - The Orin keeps `zidane.jpg` and thor1 keeps `ppe-hse-factory.png` as their image pins.
  - The Orin's models are as before its deployment: 3 Triton READY, and its vLLM model FAILED, as in the pre-existing observation below.
  - The device login files are shredded.

### Observations (pre-existing, not caused by this change)

- **Live stream.** The `/streams` broadcaster opens cameras through Aravis directly, so it answers 503 for both virtual cameras. The image camera behaves the same way on base. It is not one of the spec's Frame_Consumers.
- **Deleted Image_Sources.** A locally deleted Image_Source stays in the camera-registry shadow and in the Portal registry. The agent only retires a fixed key set, and shadow updates merge. A temporary preview Image_Source from this run (`cfg-w2whc2as` on the Orin) was affected; it was cleaned up with a `null` write, as recorded under "Left as found".
- **Removed workflow components.** Removing a workflow component from a deployment leaves its `/aws_dda/workflows/<id>` artifacts and its registration on the device.
- **Static image panel.** The panel's metadata comes from the latest Portal pin request. After a device-API pin it lags the device's actual image. The registry row catches up at the next inventory report, which after a device-API replace can be much later (see Deviations).
- **Orin log noise.** On the Orin, the Plugin checksum errors for two other workflows (`f81a4c66`, `0c7fe31a`) were already logged about 1,436 times an hour on 1.0.69. The `emlcapture.cpp CHECKIF strlen(meta)` capture traces also appear in thor1 logs from August.
- **Portal deployments.** A Portal deployment adds the `workflowTuning` LocalServer configuration (bucket `dda-inference-results-164152369890`), which comes from a sibling feature.
- **Stale physical-camera keys.** thor1's camera-registry shadow still lists `arv-797b019251e9` and `arv-cf582dea7590`, both Baslers, as present. Both entries were last written on 2026-09-22, and one of those cameras is now on the Orin. The agent does not retire keys it stops reporting, and shadow updates merge. This is the same cause as "Deleted Image_Sources" above.
- **Orin vLLM model.** After each backend start, `qwen2-5-vl-7b-instruct-awq` loads, reports READY, unloads, and is then `preflight-refused` for memory starvation (4.56 GiB available). The same sequence is in the device's 1.0.69 logs from 2026-09-25 01:37Z and 01:58Z. The three Triton models are READY.

### Deviations

- **Requirement 6.4.** It says "a workflow SHALL be able to bind different nodes to each" virtual camera. The existing Workflow_Validator rule `V7_COEXISTENCE_CONFLICT` allows one `aravis_camera_source` per workflow, because the single-frame appsrc Frame_Feed supports one. This was verified as one workflow per camera, running concurrently on one device.
  - The user agreed to deliver multi-source workflows in their own spec, `.kiro/specs/multi-source-workflows/`, whose Requirement 8.3 closes this clause.
- **Requirement 4.6 after a device-API replace.**
  - The requirement: the inventory entry reports the Pinned_Video's metadata.
  - The gap: after a replace through the device's `/static-video-camera/pin`, the entry keeps the previous video's metadata until something else triggers a camera-registry report. Such triggers are an enumeration change, an Image_Source change, a Portal change, or a restart. On the Dell that took 33 minutes.
  - What is not affected: the frames served, workflow binding by camera id, and Portal-initiated pins and replaces.
  - Cause: the device pin endpoints do not call `camera_sync.hooks.notify_image_source_changed()`. The image camera's endpoint has always behaved this way; this spec left it unchanged per Requirement 6.3.
  - The fix is that one call after a successful video pin or unpin. It would need a rebuild and on-device re-verification before commit.
  - **Fixed in task 14.6.** Every variant was rebuilt. On all four devices, a device-API pin, replace or unpin now reaches the shadow within about 5 s, and 30 of 30 soak replaces did so on each. The image camera's device endpoint keeps its old behavior, per Requirement 6.3.
