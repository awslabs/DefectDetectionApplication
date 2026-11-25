# RF-DETR-Seg — verification notes (Requirements 18.5, 18.6; tasks 23.1–23.3)

Spec `.kiro/specs/rfdetr-seg-support` (requirements, design, tasks). Implementation run 2026-11-22 → 2026-11-25 on the
branch `feature/rfdetr-seg-support`; Portal deploys 2026-11-23 (feature) and 2026-11-24 / 2026-11-25 (feature floor);
LocalServer builds 2026-11-23 → 2026-11-25; on-device runs 2026-11-24 / 2026-11-25. Devices are named by their logical
names only; the run's own records (device state, job ids, cleanup ledgers) live outside the repository.

## 1. Shipped per architecture (Requirement 18.6, task 23.3)

| arch id | LocalServer component, version in the floor | basis | declared shipped? |
|---|---|---|---|
| `arm64_jp6` | `aws.edgeml.dda.LocalServer.arm64JP6` **1.0.85** | hardware: the lab JP6 Orin devkit, group 22 (all four model paths, both modes, stability, zero case, oracle, Results tab), 2026-11-24 / 25 | **yes** |
| `arm64_jp5` | `aws.edgeml.dda.LocalServer.arm64JP5` **1.0.62** | the spec's stated coverage for JP5 (Req 14.5, 18.1): container unit tests in the built image plus the in-job Fleet_Floor_Runtime check (§4); in addition the same device code was exercised on JP5 hardware on the demo-account JP5 rig, which runs its own account's build of the same source (§2.3) | **yes**, with the per-account floor limit of §8 (M-11) |
| `x86_64`, `x86_64_nvidia` | `aws.edgeml.dda.LocalServer.amd64` **1.0.56** (one component serves both arch ids) | container unit tests + the in-job Fleet_Floor_Runtime check (§4) | **yes** (the coverage the spec states for these archs) |
| — | `aws.edgeml.dda.LocalServer.amd64Nvidia` **1.0.5** | rebuilt and published (O11 a); outside `ARCH_TO_LOCAL_SERVER_COMPONENT`, so not read by the floor | recorded only |
| `arm64_jp7` | no floor entry; candidate build `aws.edgeml.dda.LocalServer.arm64JP7` **1.0.60** published, unverified | no JP7 device in the window (group 21 deferred, owner decision L5); container unit tests and the in-job floor check passed in the build; same device-side code as the JP6 build | **no** — `arm64_jp7` is in `UNVERIFIED_RFDETR_SEG_ARCHES`; a Seg workflow for it is refused (`RFDETR_SEG_UNSUPPORTED_ARCH` at packaging, the deployments 409); follow-up = tasks 21.1–21.3 then 23.1 |
| `arm64_cpu` | none (`aws.edgeml.dda.LocalServer.arm64` was not built: no Portal build target, no Build Fleet job has ever built it) | — | **no** — listed in `UNVERIFIED_RFDETR_SEG_ARCHES` (task 18.6 fallback, owner decision C5b), refused the same way |

Why "yes" for JP5 / x86_64 / x86_64_nvidia without hardware: Requirement 18.1 (O11 a) names hardware verification for one
JP7 and one JP6 device and states container unit tests plus the in-job Fleet_Floor_Runtime check as the coverage for the
other archs; §4 records that coverage. Why "no" for JP7: Requirement 18.6 — no device was online in the window, so the
entry was removed and the arch listed as unverified (Req 14.8 d iii); the feature cannot be used on JP7 until a device
verifies the 1.0.60 build and the entry is re-added (tasks 21.1–21.3, 23.1 follow-up, kept open in `tasks.md`).

Do not deploy: `aws.edgeml.dda.LocalServer.arm64JP6` **1.0.84** and `aws.edgeml.dda.LocalServer.arm64JP7` **1.0.59**
(published 2026-11-24, every request answers HTTP 500 — see §3).

## 2. Devices

### 2.1 JP7 — none (deferred)

No JP7 lab device existed in the window: the device of the 2026-09-22 detection verification is gone and the only other
JP7 unit is a travelling show device this run did not touch (owner decision L5). Tasks 21.1–21.3 did not run. What exists
for JP7: the Seg-capable build `arm64JP7` 1.0.60 (lab Portal account, published 2026-11-25; its first build 1.0.59 is
unusable, §3), whose in-container unit tests and in-job Fleet_Floor_Runtime check passed, and the JP6 verification of the
same device-side code. What does not exist: any inference of a Seg model on a JP7 device. The JP7 oracle
(`model-blue-plate-rfdetr-small-jetson-xavier-jp7`) was therefore not re-run; the same record's JP6 component was (§2.2).

### 2.2 JP6 — the lab JP6 Orin devkit (lab Portal account; 2026-11-24 → 2026-11-25; groups 19, 22)

- **LocalServer**: `aws.edgeml.dda.LocalServer.arm64JP6` **1.0.85** (the rebuild with the `sentry-sdk<3` pin; the device
  was found at 1.0.81 and keeps 1.0.85 afterwards, owner decision L5). Proof the new code ran: 36 of 36 backend files
  changed by this feature match the built commit by sha256 inside the running container; the Alembic Seg revision ran at
  start. The first attempt deployed 1.0.84 and every request answered 500 (§3); the device was rolled back to its saved
  deployment and the 1.0.84 run was recorded as a dry run only, not as verification.
- **Models under test (22.1)** — the four paths of Requirement 18.2:
  - (a) **trained**: a DDA labeling job in the new `InstanceSegmentation` modality (63 images, `grounded-sam`
    pre-labels kept per instance, instance manifest) → a Portal training job (test mAP@50 0.969, mask mAP@50 0.969) →
    packaged and published as a JP6 component 1.0.0 (a test component, removed at cleanup) → deployed through
    `POST /deployments` → **instance** mode: 10/10 runs, 3 instances per frame with `mask_rle`, overlay image with masks
    and boxes. Mask IoU against the labeled ground truth: 0.983 / 0.975 / 0.980 (image 1), 0.984 / 0.984 (image 2; one
    labelled instance had no matching prediction), 0.983 / 0.971 (image 3) — seven matched instances, all ≥ 0.97.
  - (b) **re-packaged** semantic: the same record as component 2.0.0 with `output_mode = semantic` (Package action
    override): deployed manifest stage `rf_detr_semantic_segmentation`; `mask.png` beside the capture;
    `internal-color-map` with `background` + `cookie` (33.8 % area); the LFV-shaped record (`anomaly-mask-ref-detected`,
    `groundtruth/semantic-segmentation`, `anomalies` map) — 10/10 engine runs and 3/3 legacy-workflow runs, identical in
    shape to the LFV segmentation result of the pre-Seg path on the same device.
  - (c) **converted**: the published Seg-Nano `.pth` through Smart Import. Parity_Check metadata: `parity.mask_iou_min`
    **1.0** on both runtimes (ORT 1.16.3 and 1.30.0), box max |Δ| 1.19e-7, score max |Δ| 2.04e-6 (rfdetr's own `predict`
    is the parity reference, off-device). On device 10/10 runs; its boxes / masks against the imported official ONNX
    export of the same `.pth` on the same image: box ≤ 0.06 px, score ≤ 7.2e-5, mask IoU ≥ 0.9999 — within the task 7.4
    tolerances. The (a) job's `checkpoint_best_total.pth` was converted the same way (parity recorded).
  - (d) **imported ONNX**: the 19.1 replacement component `model-rf-detr-seg-nano-semantic-jetson-xavier-jp6` 1.0.0
    (semantic, 90 COCO names): 10/10 runs, a semantic mask overlay for the pinned image (`maskImage` 13,955 bytes;
    `person` 42 % of the frame, `tie`) — the first recorded inference of that model family on JP6; and the instance twin
    `model-rf-detr-seg-nano-instance-jetson-xavier-jp6` 1.0.0: 3 instances `person` 0.954 / `person` 0.943 / `tie`
    0.813 with `mask_rle` — the labels are the supplied COCO names, not `class_i`.
- **Manifests (both modes, 19.1 components)**: stage `rf_detr_semantic_segmentation` / `rf_detr_instance_segmentation`; a
  top-level `detection` block with `output_mode` `semantic` / `instance`, `output_names` = the graph's `pred_masks` /
  `pred_boxes` / `pred_logits`, `top_k 100`, 90 `class_names`, `pixel_level_classes.names` of length 91 (`background` +
  the 90 names); no stage-level `detection`; records `detection_arch rf_detr_seg`.
- **Stability, zero case, history, edge MCP (22.2)**: ≥ 10 workflow runs per model (19.1 semantic ×10, instance import
  ×10, trained instance ×10, trained semantic ×10, converted ×10, detection oracle ×10, plus a 40-run legacy instance
  workflow) with the backend healthy and no restart between runs. Zero case (an image with no objects): `detection_count
  0`, empty `detections`, no `mask.png`, `output = 0`, confidence 0.0, blank overlay. Result history: instance rows carry
  the `detections` column (boxes + `mask_rle`), the per-capture detail answers 200, the summary counts them as
  detections, both export manifests download (classification; instance with `instance-segmentation` lines). Edge MCP:
  `edge_results_list` returns the rows with `bounding_box` + `mask_area`, `mask_rle` / `maskImage` dropped (named in
  `dropped_fields`); `edge_result_get` returns the composite mask as an image part (`McpServerEnabled` merged for the
  check and reset afterwards).
- **Detection oracle (Req 18.4)**: the RF-DETR detection record `blue-plate-rfdetr-small` (2026-09-22), published for
  JP6, still returns its three `blue_plate` boxes — **0.950 / 0.947 / 0.942** on this device (JP7 reference 0.951 / 0.940
  / 0.939; IoU against the labelled boxes 0.973 / 0.970 / 0.893): the detection decoder and the result contract did not
  drift.
- **S6 timing (design §24.6, measured on the device, CPU post-processing, numpy 1.26.4 / OpenCV 4.11.0, MAXN)**: 10
  instances — decode 32 ms, unpack + paint composite 8 ms, unpack + RLE encode 85 ms, bare unpack 3 ms, `mask_rle` text
  50 KB; 100 instances — 155 ms / 75 ms / 802 ms / 30 ms / 501 KB. Written into `docs/multi-runtime-inference.md` §24.6
  beside the x86_64 spike figures.
- **Results tab (22.3; Req 18.5)**: against the device's captures uploaded to the use case's inference-results bucket (52
  captures, 38 with masks, instance rows at 3 instances each): light 50-capture page **2.54 s / 278,026 bytes**,
  20-capture page 0.47 s / 105,170 bytes, one per-capture detail (`include_masks=1`) **0.16 s / 14,948 bytes** — far below
  the 20 s and 5 MB contingency thresholds, so `ResultsTab.tsx` `limit` and `DETECTION_LIST_MAX_BYTES` are untouched.
- **Feature-floor gate seen live (20.2)**: a workflow with the 19.1 Seg model packaged for `arm64_jp6` (201,
  `has_rfdetr_seg_model: true`) and deployed through `POST /deployments` (201, `is_revision`); the same workflow for
  `arm64_cpu` and for `arm64_jp7` refused with 409 `RFDETR_SEG_UNSUPPORTED_ARCH`, no artefact written. The
  deployments-side 409 for an omitted arch was not exercised (no enrolled device of an omitted arch); its unit tests
  stand.
- **How the HMI run preview and the Portal `ResultsViewer` were checked**: through their API payloads (`/overlay`:
  `instances`, `maskImage`, `maskBackground`, `detectionsTruncated`; the overlay image; the Results-tab page / detail
  bodies) and the built HMI bundle's strings (`Mask area (px)`, `Masks + boxes`, `Mask coverage`) — not in a browser (the
  verification host has none).
- **Device left as found** (plus LocalServer 1.0.85): same feature-configurations (3 operator models READY), the
  previously pinned static image restored (sha256 identical), same image sources, workflow registrations, live and
  effective configuration hashes, Triton repository; every test component, workflow, capture, export manifest, shadow
  key and Portal record of this run removed; the 19.1 records / components kept as deliverables.
- **Platform finding (outside this spec, not Seg-specific)**: swapping one model component's version in place (1.0.0 →
  2.0.0) while the LocalServer runs leaves the model-state cache inconsistent (READY in the list view, UNAVAILABLE in
  the describe view, pipelines refusing); a LocalServer restart clears it.

### 2.3 JP5 — the demo-account JP5 rig (second Portal topology; 2026-11-25; the JP5 analogue of 21.1 d, 21.2, 21.3)

- **LocalServer**: that account's own `aws.edgeml.dda.LocalServer.arm64JP5` lineage, at its **1.0.2** build of the 18.3
  source (Seg-capable; 23 of 23 device files changed by this feature match the built commit by sha256 inside the running
  container; onnxruntime 1.16.3 from the staged wheel; the Alembic Seg revision ran at start). The rig had no models and
  the demo Portal has no Export_Image, so legs (a)–(c) were not run here (covered on JP6); leg (d) and the 21.2 / 21.3
  checks ran on JP5 hardware.
- **Models (d)**: the Seg-Nano ONNX imported through the deployed Smart Import twice with the 90 user-supplied COCO
  names, `instance` and `semantic` `output_mode`, packaged and published for `jetson-xavier-jp5` (test components,
  removed afterwards). Manifests as in §2.2 (stage per mode, top-level `detection` with `output_mode`, `output_names`
  pred_masks / pred_boxes / pred_logits, `top_k 100`, 90 `class_names`, `pixel_level_classes.names` 91).
- **Instance mode**: 10/10 engine runs (~1.0 s each), 3 instances with the supplied labels — `person` 0.953 (mask
  240,127 px), `person` 0.942 (151,344 px), `tie` 0.805 (10,142 px), `detectionsTruncated: false`; every decoded
  `mask_rle` area equals the reported `mask_area`, ≥ 99.6 % of each mask inside its box; composite `maskImage` PNG
  (13,931 bytes) == the union of the instance masks (IoU 1.0), background 57.66 % == `maskBackground`; the composite is
  painted ascending by confidence (the design's higher-confidence-wins rule). JP6 gave 0.954 / 0.943 / 0.813 on the same
  model and image.
- **Semantic mode**: 10/10 runs; the LFV-shaped record (`Anomaly`, `anomalies` colour map `person` 42.34 % + `tie`,
  `mask.png` 1280×720 beside the capture); the semantic person fraction equals the instance person-union fraction.
- **Zero case**: engine ×3 and legacy ×3 — `detection_count 0`, `{"detections": {}}` sidecar, no `mask.png`, confidence
  0.0, blank overlay. Backend healthy throughout, no restart between runs.
- **History / exports / edge MCP**: 10 legacy instance runs — history rows with the `detections` column, per-capture
  detail 200, summary `detection: 10`; both exports download (classification manifest 10 lines; instance manifest 10
  lines of `instance-segmentation`, 3 annotations each, class map `{0: person, 1: tie}`); semantic ×4, zero ×3. Edge
  MCP: 45 read-only tools, `edge_results_list` with `bounding_box` + `mask_area` and `mask_rle` / `maskImage` in
  `dropped_fields`, `edge_result_get` with the composite mask as an image part. The detection oracle was not re-run here
  (no detection record exists in that account).
- **Results tab (second topology; 17 captures mirrored and removed)**: light 50-capture page **2.29 s / 87,883 bytes**
  (10 `has_masks`), 20-capture page 0.90 s / 87,873 bytes, one detail with masks **0.22 s / 14,908 bytes** — no
  contingency; semantic captures expose `mask_url` with `has_masks: false` (the instance flag), as designed.
- **Floor finding (M-11)**: the deployed floor literal names the lab account's versions (`arm64_jp5: 1.0.62`); on this
  topology the `arm64JP5` lineage is `1.0.0 → 1.0.2`. The Portal packaged a Seg workflow for `arm64_jp5` (201,
  `has_rfdetr_seg_model: true`) stamping `1.0.62` into the manifest and the recipe's hard LocalServer dependency, and
  `POST /deployments` answered 409 `INCOMPATIBLE_LOCAL_SERVER` — the gate works as specified, against a version that
  lineage cannot reach. The device exercise used rig-adapted copies of the three Portal packages (only the
  `minLocalServerVersion(s).arm64_jp5` stamps and the recipe's LocalServer version requirement lowered to `1.0.2`, every
  other byte identical) added by Greengrass copy-and-edit; after the restore the device's own `validate_artifact` marked
  those registrations invalid ("Requires LocalServer >= 1.0.2") — the device-side gate seen live. Follow-up in §9.
- **Restored (owner decision C2)**: the pre-test deployment revision was restored — same component set and versions
  (the previous LocalServer version runs again), the merged configuration with `reset` for the one key this run merged,
  the four pre-existing test workflows registered again (registration document sha256 equal), the previously pinned
  static image byte-identical, metadata DB back at its pre-Seg Alembic revision; every Portal record, component, S3 object
  and temporary identity of this run removed.

## 3. LocalServer builds (group 18) and the feature floor (groups 20, 23.1)

| component | account | version | source | note |
|---|---|---|---|---|
| `aws.edgeml.dda.LocalServer.amd64` (18.4) | lab | **1.0.56** | G1 tree | floor keys `x86_64` + `x86_64_nvidia` |
| `aws.edgeml.dda.LocalServer.amd64Nvidia` (18.5) | lab | **1.0.5** | G1 tree | outside the arch map |
| `aws.edgeml.dda.LocalServer.arm64JP5` (18.3) | lab | **1.0.62** | staged-wheel head | floor key `arm64_jp5` |
| `aws.edgeml.dda.LocalServer.arm64JP5` (18.3, second topology) | demo | **1.0.2** | same source | for the demo-account JP5 rig |
| `aws.edgeml.dda.LocalServer.arm64JP6` (18.2) | lab | 1.0.84 → **1.0.85** | staged-wheel head → + `sentry-sdk<3` | 1.0.84 unusable; 1.0.85 verified (group 22), floor key `arm64_jp6` |
| `aws.edgeml.dda.LocalServer.arm64JP7` (18.1) | lab | 1.0.59 → **1.0.60** | staged-wheel head → + `sentry-sdk<3` | 1.0.59 unusable; 1.0.60 candidate, unverified, no floor entry |
| `aws.edgeml.dda.LocalServer.arm64` (18.6) | — | none | — | no Portal build target → `arm64_cpu` unverified |

Final floor literal (`compute-stack.ts` `WORKFLOW_RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS`, deployed to both Portals
2026-11-25): `{"arm64_jp5": "1.0.62", "arm64_jp6": "1.0.85", "x86_64": "1.0.56", "x86_64_nvidia": "1.0.56"}`;
`UNVERIFIED_RFDETR_SEG_ARCHES` = `arm64_cpu`, `arm64_jp7` (tests `test_rfdetr_seg_feature_floor_coverage.py`,
`rfdetr-seg-infra.test.ts`). Candidate → confirmed per arch (task 23.1, confirmed 2026-11-25 against the device records):
`arm64_jp5` candidate 1.0.62 → confirmed 1.0.62; `arm64_jp6` candidate 1.0.84 → raised to the fixed build 1.0.85 (Req
14.8 d ii, the failed build stays below the floor) → confirmed by group 22; `arm64_jp7` candidate 1.0.59 → removed (Req
14.8 d iii; 1.0.60 is the candidate the follow-up enters); `x86_64` / `x86_64_nvidia` 1.0.56 → confirmed (both ids map to
the `.amd64` component; the `.amd64Nvidia` 1.0.5 build sits outside the map and is recorded here only). The 21.3 / 22.3
contingencies (`limit: 20`, a lower `DETECTION_LIST_MAX_BYTES`) were not triggered (§2.2, §2.3).

Two build-pipeline changes outside the spec's task list were made under owner decisions, in their own neutral commits:
- **C5 / C5-alt — staged onnxruntime wheels with a pinned-commit source fallback.** The upstream onnxruntime repository
  answered 404 from 2026-11-17, so every Jetson image build failed at its onnxruntime source clone. `build-custom.sh` now
  downloads the target's onnxruntime-gpu wheel (1.16.3 / 1.20.1 / 1.23.2, taken from the fleet's previously built images,
  sha256-pinned in a committed manifest, fail closed on mismatch) from the account's component bucket, and
  `install_onnxruntime_gpu.sh` installs a staged wheel when present and otherwise clones the `intel/onnxruntime` fork at
  the exact upstream release commit per version (pinned in the same manifest; the checked-out `VERSION_NUMBER` is
  verified). Static tests cover both. The three Jetson builds above come from the staged-wheel path; x86 and every Portal
  path are unchanged (not a Requirement 17.1 departure).
- **C6 — `sentry-sdk<3` in the JP6 / JP7 vLLM layer.** 1.0.84 / 1.0.59 pulled an unpinned `sentry-sdk`; its 3.0.0
  release (2026-11-23) removed an import the request middleware (`asgi-correlation-id` 4.2.0) needs, so every request
  answered 500. The pin (plus a static test) is the smallest fix; 1.0.85 / 1.0.60 are the rebuilds. JP5 / amd64 /
  amd64Nvidia images have no vLLM layer and were never affected.

Known difference of the staged-wheel Jetson images built before the tooling fix (`arm64JP6` 1.0.84, lab `arm64JP5`
1.0.62, demo `arm64JP5` 1.0.2): `/usr/bin/git` and the pip `cmake` extra are absent (build-only tooling; nothing on a
device uses them; the installed wheel is byte-identical to the source-built one). The rebuilt 1.0.85 / 1.0.60 carry the
tooling again.

## 4. Coverage statement — JP5, x86_64 CPU / Nvidia, arm64 CPU (Req 14.5, 18.1)

- **Container unit tests.** Task 16.2 ran all 781 device suites in the JP6 image (8,371 passed, 0 regressions against
  the base) and the named Seg suites in the JP5 image; every published build also ran its own late in-container gate
  (the device suites and `test/backend-test/security/preservation`) inside the freshly built image — green in each of
  the seven fleet jobs above.
- **In-job Fleet_Floor_Runtime check (task 7.3).** Every Conversion_Job runs the exported Seg graph on
  `/opt/ort-floor` onnxruntime **1.16.3** beside the image's own 1.30.0. On the pushed Export_Image digest the fixture
  matrix converted the Seg-Nano `.pth` (`dets` / `labels` / `masks`, parity mask IoU 1.0, floor runtime ok), and 22.1 (c)
  recorded `parity.mask_iou_min` 1.0 on 1.16.3 for the published checkpoint and the trained one.
- **Basis (spike S2, task 0.2)**: "onnxruntime 1.16.3 loads and runs the raw rfdetr 1.10.1 Seg-Nano export (IR 8, opset
  17) — run output `[1, 100, 78, 78]` float32, byte-identical to onnxruntime 1.30.0's after the static-dim stamp; S4 mask
  IoU min 1.0 on both runtimes." The 0.3 floor contingency (an operator / opset the floor runtime rejects) did not fire,
  so no arch is unverified for that reason. The 1.19.2 contingency did not fire either (the trainer's runtime opens the
  capped graph).
- `arm64_cpu` is unverified for a different reason — no Seg-capable build exists (§3) — and stays refused.

## 5. Portal deploys (group 17, 20.2) — both topologies

- **17.1 Export_Image**: rebuilt from the tree (the Conversion_Job runs the code baked into the image), pushed as
  `dda-detector-export@sha256:5faa0e62…` (2026-11-23), code in the image == tree; SSM `/dda-portal/detector-export-image`
  moved v1 → v2 to that digest in the lab account and `ModelConverterHandler`'s `DETECTOR_EXPORT_IMAGE` equals it (re-read
  at 22.3). Fixture matrix on the digest: the Seg-Nano `.pth` converts; the YOLO-seg fixture still fails with its `FATAL:`.
  The demo Portal account has **no Export_Image configured** (empty before and after; the `.pth` conversion path is
  unavailable there, the ONNX Smart Import path is available) — an owner follow-up (§9).
- **17.2 single-account deploys (2026-11-23)**: lab, then the demo Portal (owner decision L3), tree `a50c9391`, flag-less,
  13 stacks each, rc 0. Verified per account: `PORTAL_RFDETR_SEG_ENABLED=true` on exactly `TrainingHandler`,
  `ModelConverterHandler`, `PackagingHandler`; `WORKFLOW_RFDETR_SEG_MIN_LOCAL_SERVER_VERSIONS={}` on exactly
  `DeploymentsHandler`, `WorkflowPackagingHandler` (fail-closed until group 20); `CapturesHandler` `MemorySize` 1024;
  the `RfDetrSegEnabled` output; `config.json` `rfDetrSegEnabled: true`; the bundle carries `rf_detr_seg`, "Instance
  Segmentation (RF-DETR-Seg)", "Masks unavailable for this capture"; the SharedLayer carries the new symbols; the
  Grounded-SAM worker still present; deployed == tree by sha256 for the 20 changed files; 401 / 403 probes; a 30-minute
  CloudWatch watch with 0 errors. **Deployed MCP**: `MCP_SERVER_VERSION=1.4.0`, and a live `initialize` reports
  `serverInfo.version 1.4.0` (89 tools; the audit row's `mcp_server` suffix `@1.4.0`) on both Portals. The demo deploy
  also shipped the `main` commits newer than its deployed bundles (owner decision L3; listed in the run record).
- **17.3 topologies**: neither Portal runs the use-case-account topology (both single-account), so there was no deployed
  use-case-account stack to diff or deploy. The use-case-account stack set is unchanged by this branch (no IAM statement
  change, `usecase-account-stack.ts` untouched; a synth at HEAD recorded 21 resources). The expected single-account diff
  was recorded and classified line by line for both Portals: the three environment literals, the `RfDetrSegEnabled`
  output, the one `MemorySize` on `CapturesHandler`, the code / layer asset updates of every edited handler and the
  SharedLayer, the `TrainingHandler` bundle; no IAM / Cognito / authorizer / table / KMS change. The deployed stacks
  already carried the incoming `WorkflowsHandler` 1024 (BD4), so no such line appeared.
- **20.2 floor deploys**, both Portals, flag-less, template-only diff = exactly the two Lambda environment changes plus the
  SageMaker custom-resource timestamp: (i) 2026-11-24 with the candidate literal; (ii) 2026-11-25 with the final literal
  (§3); post-deploy verification 14/14 per account each time (deployed literal == `compute-stack.ts`, deployed handlers ==
  tree by sha256, no other Lambda carries the literal).
- **ComputeStack template size (14.6)**: 672,230 bytes minified / 247 resources at the 16.4 gate (limit 1 MB / 500);
  695,222 bytes processed (669,922 bytes synthesised) read back at 22.3.

## 6. Gates (group 16) and the preservation suite (16.5)

- Preservation suite (`test/backend-test/security/preservation`) at the gate head vs the 43cd4ff1 baseline: host 183
  passed / 4 skipped (baseline 183 / 4); JP6 image 177 passed / 10 skipped (like-for-like 174 / 13 = baseline); JP5
  image 163 / 12 (like-for-like 160 / 15 = baseline). Reproduced after the `main` merge and before every push.
- Full lanes at the gate head: Portal backend 567 suites (every failure identical at the base, none attributable to the
  branch), `tests/mcp` 4,078 passed (counts 89 / 88 / 31 held, version literal 1.4.0 in all sites), infrastructure jest
  585 passed with the IAM-preservation synth gate and the drift guards, Portal vitest 3,069 passed + build, HMI Jest 365
  passed + build; CSI goldens untouched. Pin checks: 61 backend, 54 device and 42 frontend pinned ranges — 0 findings;
  every moved pin belongs to the task that names it (no pin moved outside its named task).
- `packaging.py` and `model_converter.py` rebaselined in `iam_out_of_scope_baseline.json` with notes naming this spec
  (Req 17.4); no other pinned file changed.

## 7. Replacement component (19.1) and the harness rename (19.2)

The published JP6 Seg component was replaced by a re-import through the hardened Smart Import (D5): the same
Seg-Nano ONNX, `model_type instance_segmentation`, 90 COCO class names, thresholds 0.5 → a new record (`detection_arch
rf_detr_seg`, gated by DD8) published as **`model-rf-detr-seg-nano-semantic-jetson-xavier-jp6` 1.0.0** (lab Portal
account, 2026-11-24); the instance-mode twin **`model-rf-detr-seg-nano-instance-jetson-xavier-jp6` 1.0.0** for 22.1 (d).
The old record and `model-rf-detr-seg-nano-jetson-xavier-jp6` 8.0.0 are left in place. `test/on-hardware/harness/README.md`
and `devices.yaml.example` name the new component as the JP6 harness's expected vision model.

## 8. Departures, additions, mismatches and behaviour notes for the reader

- **Owner-accepted departure from Requirement 17.1 (OR-1 / M-3)**: the rfdetr trainer requirements now pin
  `transformers==5.17.0`; detection training on the current trainer image depends on it (the unpinned resolution broke
  `import rfdetr`; the pin is the Export_Image lock's version). Every RF-DETR detection training job's source bundle
  changes by that one line; the SageMaker request shape, `train_rfdetr.py` and the YOLO path are untouched; static test
  `test_requirements_rfdetr_pins_transformers`.
- **Owner-directed additions outside the spec**: C5 / C5-alt (staged wheels + pinned-commit fork fallback) and C6
  (`sentry-sdk<3`), §3.
- **Spec-versus-code mismatches** (all resolved as text-only corrections before the code, none touching a D/O decision):
  M-1 the raw 1.10.1 export declares the mask dims symbolically → one post-export static-dim stamp; M-2 published Seg
  weights are a stripped `{"model"}` checkpoint → the state dict is the authoritative task signal; M-3 → OR-1; M-4
  sequencing of `RFDETR_NON_DETECTION_PREFIXES`; M-5 the polygon tool's own undo stack; M-6 a third modality list in the
  LLM preview arm; M-7 an unknown `manifest_format` is answered **400** (the device app's validation handler), not 422;
  M-8 the `transformManifest` union stays `classification | segmentation`; M-9 the ADMIN_GUIDE "Versions" row and the
  CHANGELOG entry; M-10 one deployments stub gains `rfdetr_seg=False`; M-11 per-account floors (§2.3, §9).
- **Behaviour notes**: the HMI instance-mask layer (run preview, live result, history detail) renders only when some
  instance carries a `mask_rle` (not merely when `instances` is non-null), so plain detection pages are unchanged; each
  surface paints over its own image (the run's output image; the capture's input image) and a size mismatch degrades to
  "mask skipped". Skip-verification labeling jobs are not offered for `InstanceSegmentation` (400: no Bedrock pre-labeler
  supports it). The Seg_Switch is configuration-only (default ON; per-Lambda `PORTAL_RFDETR_SEG_ENABLED`; frontend
  `rfDetrSegEnabled` absent = ON). The model component's Startup script can log "not READY" while the engine is still
  loading and settle READY a minute later — the known startup race, not Seg-specific.

## 9. Open items (Requirement 18.6 — left visible, never silently dropped)

1. **`arm64_jp7` unverified; tasks 21.1–21.3 open.** Follow-up as written in `tasks.md` group 21: "when the owner names a
   JP7 device, enter the rebuilt 18.1 build's version (`1.0.60`) as the `arm64_jp7` candidate entry and remove the arch
   from the unverified set in one change (enablement order step (i)), redeploy both Portals, run 21.1–21.3 on it, then
   confirm the entry in 23.1 (or raise it to a fixed build, 14.8 d ii)." Until then a Seg workflow for `arm64_jp7` is
   refused. Do not deploy `arm64JP7` 1.0.59.
2. **`arm64_cpu` unverified** (task 18.6): "the Portal Build Fleet has no build target for this component and no fleet job
   has ever built it, so `arm64_cpu` is listed in `UNVERIFIED_RFDETR_SEG_ARCHES` with that reason". A Seg-capable
   `aws.edgeml.dda.LocalServer.arm64` build (an arm64 CPU host, `portal-build.sh aarch64 cpu`) and a verification would
   be needed before an entry can exist.
3. **Per-account feature floors (M-11)**: "the feature floors are per-account facts and would need per-deployment
   configuration (CDK context / SSM) to enable Seg deployments on a topology whose LocalServer lineage differs from the
   lab's; until then a Seg workflow is packageable but not Portal-deployable there." The pre-existing stream-camera floor
   has the same property. A later spec.
4. **Demo Portal without an Export_Image**: "`ModelConverterHandler` `DETECTOR_EXPORT_IMAGE` unchanged, empty: this account
   has never had an Export_Image configured … the `.pth` Smart Import conversion path is therefore unavailable on the demo
   Portal before and after this deploy." Owner decision whether to build / configure one there.
5. **Do-not-deploy builds**: `arm64JP6` 1.0.84 and `arm64JP7` 1.0.59 stay published but unusable (§3); nothing in the
   Portal marks them — an operator note until they are retired.
6. **Platform finding, outside this spec**: the in-place model-component version swap leaves the model-state cache
   inconsistent until a LocalServer restart (§2.2).
7. **Pre-existing test failure at the base, outside this spec** (recorded by unit u02 for the report):
   `tests/test_dda_labeling_rbac_role.py::test_user_admin_portal_roles_include_data_labeler` fails at the base against
   `user_admin.validate_create_request`'s `given_name` requirement (an earlier PR) — identical at the base and at the
   head, not touched by this feature.
8. **Build infrastructure observation, outside this spec**: the demo account's dedicated JP5 / JP6 build server's SSM
   agent stopped processing commands after each connectivity change during the temporary NAT gateway on / off cycles
   (owner decision C1) and recovered only with a reboot; the other builder on the same subnet did not. Not changed by this
   run.

Spike items (S4–S7) whose answer changed a design default: **S5** — the trainer requirements needed the `transformers`
pin (M-3 → OR-1); **S6** — the design's provisional post-processing cost figures were replaced by the measured x86_64 and
on-device numbers in `docs/multi-runtime-inference.md` §24.6. S4 (parity tolerance 0.99 holds; measured 1.0) and S7 (all
six Seg sizes resolve) confirmed the defaults. Earlier spike items: S1 → M-2 (probe envelopes), S2 → M-1 (static-dim
stamp); the S2 floor-runtime stop rule was not triggered.

Pins moved outside their named task: none (16.x pin checks; the one stub outside the spec's test ledger,
`test_workflow_rbac_audit.py`, was added to task 14.4's "Pins that move" by M-10 before the code).

Contingencies applied in this run: Req 14.8 (d) ii for `arm64_jp6` (1.0.84 → 1.0.85); Req 14.8 (d) iii for `arm64_jp7`
and `arm64_cpu`; owner decisions C1 (temporary Build Fleet NAT gateway in the demo account, on / off by scoped deploys,
off at the end), C2 (restore of the demo-account JP5 rig), C3 (two re-logins of the demo-account session), C5 / C5-alt
and C5b, C6 (§3); the 20.2 redeploys of 2026-11-25 ran while a fleet build ran on a remote Build Fleet server (its own
clone; this host's `cdk.out` was moved aside; the BuildFleet stack was in the no-difference set). Not triggered: the 0.3
floor-runtime (1.16.3) and 1.19.2 contingencies, the 21.3 / 22.3 page-time and body-size contingencies.

Owner placeholders of the plan, now filled: JP6 device = the lab JP6 Orin devkit; JP7 device = none in the window
(deferred); the JP5 run = the demo-account JP5 rig (outside the numbered groups); floor values as in §3; the replacement
component = `model-rf-detr-seg-nano-semantic-jetson-xavier-jp6` 1.0.0 (+ the instance twin).
