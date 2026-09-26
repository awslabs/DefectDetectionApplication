# Run Detection Visibility: Verification Notes

## Task 6: hot-patch validation on `jetson-thor1`

Date: 2026-09-26, all times UTC.

- **Device:** `jetson-thor1` (JP7), LocalServer `aws.edgeml.dda.LocalServer.arm64JP7` 1.0.45.
- **Workflow:** `ppe-detector-v3-static-test` (`f13bb6e6-b3ae-4318-a5c7-1b1cb0f9970c:1`). It runs the imported `ppe-detector-v3` model on the Static_Image_Camera, which has the HSE factory image pinned.

### What was patched

**Backend.** `workflow_engine/run_artifacts.py`, `workflow_engine/api.py` and `endpoints/download_file.py` were copied into the backend container, which was then restarted.

- Before patching, the three device files were byte-identical to `HEAD` (`9beef02`), so the patch carries only this change.
- The sha256 values in the container after the copy match the branch files: `2c3c3d1f…`, `3443ff8e…` and `0c5262c3…`.

**Frontend.** A production build of the branch was copied into the frontend container's nginx root.

- A pristine build of `HEAD`, made with the same toolchain, reproduces the device's served bundle exactly: `main.3b88e357.js`.
- The patched bundle, `main.e53527cb.js`, therefore differs from what was running only by this change.

**Backups and reversal.** Backups are on the device under `/tmp/rdv-backup/` (the original modules and `frontend-html/`). The patch is reversed by copying them back, or by LocalServer recreating its containers from the 1.0.45 images.

### Results

**Routes on run `617776d1`** (a run from before the patch):

- `/results` → `[{"kind": "output", "hasOverlay": true, "hasOverlayImage": true}]`.
- `/overlay-image` → 200, `image/jpeg`, 276 577 B, sha256 `aa1b1c61…`. This is the run's `.overlay.jpg`.
- `/output-image` → 200, 1 039 439 B, sha256 `589e9b87…`. This is the run's `.jpg`, unchanged.
- An unknown execution → 404.
- A run with no base output image (`596c8577…`, node frames only) → 404 on `/overlay-image`.

**10 new runs.** All completed in 0.52–0.56 s. Each had:

- `hasOverlayImage: true`;
- overlay 200 (276 577 B) and base 200 (1 039 439 B), with different bytes;
- `detection_count` 10 in the metadata.

**Soak.** 20 runs, 30 s apart, from 02:54:55Z to 03:04:55Z (600 s).

- All 20 runs completed.
- `results`, `overlay-image`, `output-image` and `metadata` returned 200 for every run: 80 of 80.
- At the end: backend `running` / `healthy`, no restart after 02:48:26Z; frontend healthy with 0 restarts; all 9 models READY.

**UI.** Checked with headless Chrome through an SSH tunnel to the device's ports 3000 and 5000.

- **Results page, default view.** It shows the overlay with the boxes, labels and percentages, and the "Show bounding boxes" toggle is on.
  - Below the image is "Objects detected (10)", with the summary `helmet 2 · human 5 · no-helmet 1 · vest 2`.
  - Rows 0–9 give label, confidence and box, matching the run metadata. For example: `0 vest 80.0% x 54–207, y 279–520` and `7 human 96.4% x 766–1023, y 228–959`.
- **Results page, toggle off.** The image source switches to `/output-image`.
  - Pure-green pixels in the image area drop from 3.680 % to 0.000 %; the boxes are drawn in pure green. The raw frame is shown.
- **Run-status graph, `model_1` selected.** It shows "model_1 — output preview", the overlay thumbnail, and "Objects detected in this run (10)" listed as `label — confidence`, plus "View full results".

### Incident during the patch (not attributed to this change)

At 02:47:23Z, 11 s after the deliberate `docker restart`, the backend process aborted in native code:

```
F0000 00:00:1790390843.542850 237 forkable.cc:57] Check failed: !std::exchange(is_forking_, true)
```

- It happened while the vLLM engine was initialising and the reload script was requesting a Triton model load.
- Docker restarted the container (`RestartCount` 1) at 02:48:26Z. It came up healthy, vLLM reloaded, and every model loaded on request.
- The patched code only adds a file-existence check and a static file route. The abort is an absl fork-handler check during engine start-up.
- It did not recur during the 10-minute soak. It deserves a separate look if it shows up again after a plain restart.

### Not covered on the device

- **Segmentation (mask) runs.** No workflow registered on `jetson-thor1` runs a segmentation model with a working camera. The mask path's unchanged unit tests cover that nothing regressed (Requirement 1.4).
- **Built component.** Requirement 6.3 needs verification from a built and deployed LocalServer component. That is task 7.

## Task 7: built and deployed component on `jetson-thor1`

Date: 2026-09-26, all times UTC.

| Item | Value |
|---|---|
| Source | `wip/run-detection-visibility-jp7-verify` = `2f40e88`: `integration/all-specs` `9beef02` plus this change's 25 files, verified byte-identical to the working tree. The build log confirms "Source synced to 2f40e88". |
| Build | Portal build system, dedicated JP7 server `Jp7-24.04-pro` (`i-0345420de4f9cd214`), job `31771cf5-6b7d-4035-a519-3e7a2b2cf447`, 03:12Z → 05:40Z, **succeeded**. It took about 2.5 h because vLLM was rebuilt from source. The in-build security gates and backend unit tests passed. |
| Published | `aws.edgeml.dda.LocalServer.arm64JP7` **1.0.46**, with images `dda/flask-app:arm64JP7-1.0.46` and `dda/react-webapp:arm64JP7-1.0.46`. |
| Deployment | `6f87c51a-ec40-4b3c-88ff-a3d05037c7d7` (`jetson-thor1-localserver-1.0.46-run-detection-visibility`). It revises `33c78f0c`: all 21 components and their configuration updates are carried over, and only LocalServer changed (1.0.45 → 1.0.46). **COMPLETED** at 05:51:30Z. |

### Checks

**Containers.** LocalServer recreated the containers from the 1.0.46 images at 05:47:32Z, which replaced the task 6 hot-patch.

- The three backend modules in the container match the branch files: `2c3c3d1f…`, `3443ff8e…`, `0c5262c3…`.
- The served bundle is `main.e53527cb.js`, the same hash as the local build of the branch.

**Soak.** 20 runs of `ppe-detector-v3-static-test`, 30 s apart, from 05:54:21Z to 06:03:51Z (600 s): **20 of 20 passed.** Each run:

- completed (0.52 s warm; 6.18 s for the first, cold run);
- reported `hasOverlayImage: true`;
- returned 200 on `/overlay-image`, `/output-image` and `/metadata`, with `image/jpeg` for both images;
- served `/overlay-image` bytes equal to the run's `.overlay.jpg` on disk, and `/output-image` bytes equal to the run's `.jpg`, with the two differing from each other;
- had `detection_count` 10 and 10 Detection_List entries.

**UI.** Checked with headless Chrome through an SSH tunnel, on run `cff4767a`.

- **Results page.** It opens on `/overlay-image` with the "Show bounding boxes" toggle on, and "Objects detected (10)" with 10 rows.
- **Toggle off.** It switches to `/output-image`, and the screenshot shows the raw frame.
- **Graph.** Selecting `model_1` lists "Objects detected in this run (10)" (`vest — 80.0%`, `helmet — 93.5%`, …) with the overlay thumbnail.

**Health at 06:04:39Z.**

- Backend `running` / `healthy`, not OOM-killed; frontend `healthy`, 0 restarts; all 9 models READY; the Greengrass core device reports `HEALTHY`.
- The backend's single restart (`RestartCount` 1) happened at 05:49:54Z, during the deployment. The process logged "Local server shutdown complete; exiting" 1 s after the vLLM model reported READY: a clean exit, not a crash. It matches the 01:35:32Z exit seen after the 1.0.45 deployment earlier the same day, so it predates this change.
- After that restart, the Triton models were UNKNOWN until they were started through the feature-configurations start route, as after every backend restart.

### Coverage by architecture

**JP7:** verified on the device.

**JP5 / JP6:** not verified on a device.

- The change is architecture-independent: Python routes, and a React bundle every variant builds from the same source.
- No JP5 or JP6 build was run.

**Segmentation runs:** as in task 6, the mask path has no runnable workflow on `jetson-thor1`. It is covered by the unchanged unit tests.
