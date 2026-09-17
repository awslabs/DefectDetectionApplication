# Detection (YOLO) training in the DDA portal — implementation gap

Status: **implemented (cloud side), awaiting deploy + on-device verification.**
Everything from capture through labeling works, everything from ONNX packaging
through device deployment works, and as of 2026-09-13 the portal wiring in
§4–§8 is built under `.kiro/specs/portal-detection-training/`: `training.py`
accepts `object_detection`, validates bounding-box manifests, and launches
`datasets/detection_training/train.py` in script mode; `compilation.py` and
`packaging.py` bypass Neo and package the exported `model.onnx` with
`preserve_aspect: true`; `CreateTraining.tsx` offers Object Detection and no
longer misclassifies bounding-box manifests as Ground Truth; `compute-stack.ts`
bundles the entry point into the training Lambda. The remaining steps are the
portal deploy and the on-device check (tasks 9–10 of that spec). The sections
below are kept as the record of what was missing and why.

**RF-DETR update (2026-09-14).** RF-DETR (nano / small / medium / large) is now
a second detection architecture alongside YOLO, built under
`.kiro/specs/rfdetr-training-and-transfer-learning/`: the entry point is
`datasets/detection_training/train_rfdetr.py` (rfdetr 1.10.1, fed the COCO
`rfdetr` layout via `manifest_to_detector_dataset.py --coco-layout rfdetr`),
and its `model.onnx` is packaged under the `rf_detr_object_detection` stage
with `normalize: true`, `preserve_aspect: false` and `top_k` — no NMS and no
`iou_threshold`, the opposite geometry contract from the YOLO letterbox path
in §7. Any detection run (either arch) can now start from a base model: the
published checkpoint, or a completed portal job's own checkpoint delivered to
the entry point as `BASE_WEIGHTS_S3` + `BASE_WEIGHTS_MEMBER` (`best.pt` /
`checkpoint_best_total.pth`). Fine-tuning from an *imported* model is gated on
the findings in `docs/transfer-learning-spike.md` (Requirement 7 of that spec)
and is not wired yet. On-device verification of an RF-DETR component and of a
fine-tuned YOLO (spec task 11) is still pending.

Written 2026-09-12 from a working session on the `blue_plate` use case;
status updated 2026-09-13 and 2026-09-14.

---

## 1. What already works — do not rebuild

| Stage | Where | State |
|---|---|---|
| Device capture → S3 | `com.dda.InferenceUploader/`, or `datasets/sync_captures_to_s3.py` | works |
| Frame dedupe | `datasets/dedupe_frames.py` | works, validated |
| Bounding-box labeling | portal `CreateLabelingJob.tsx` + `dda_labeling.py` | works |
| Text-prompted pre-labels | `grounded-sam` auto-labeler | works well |
| Manifest → YOLO/COCO dataset | `datasets/manifest_to_detector_dataset.py` | works, validated |
| ONNX → Greengrass component | `packaging.package_onnx_component` | works |
| On-device ONNX serving | `OnnxRunner` + `YoloDetectionPostProcessor` | works |

`manifest_to_detector_dataset.py` is validated by
`edge-cv-portal/backend/tests/test_manifest_to_detector_dataset.py`
against manifests built with the portal's own `dda_manifest.serialize_manifest`.
It handles the DDA literal `bounding-box` attribute *and* a Ground Truth
job-named attribute, keeps negatives as explicit empty label files, stratifies
negatives across splits, and assigns whole similarity groups to a single split
so near-duplicate frames cannot leak across the train/test boundary.

---

## 2. Reference parameters for this use case

Derived from the actual capture set; a different camera framing changes them.

| Parameter | Value | Why |
|---|---|---|
| Capture resolution | `2001x2352` (aspect 0.851) | production framing, 145 images |
| **ONNX input** | **`1088x1280`** | aspect 0.850, both divisible by 32 → ~zero letterbox padding |
| `preserve_aspect` | **`true`** — mandatory | see §7; without it the frame is squashed to the input and confidence drops ~1.35x on average, 5.7x worst case |
| Classes | `['blue_plate']`, `num_classes: 1` | single class, pure localization |
| Expected ONNX output | `[1, 5, N]` | 4 box coords + 1 class score, decoded by `YoloDetectionPostProcessor` |
| `score_threshold` | start at `0.25` | the deployed model's `0.08` was a crutch for a bad model |
| `iou_threshold` | `0.45` | NMS, YOLO-only |
| Labeled data | 390 boxes / 145 images, mean 2.69 | box short side p50 = 288px at 1280 input; nothing under 32px |

Dataset: `s3://ryvan-cookies/imts-plates-luggage/`
Labels: `s3://ryvan-cookies/labeled/labeling-9cbcdb4c/output.manifest`

---

## 3. Gap: new SageMaker training entry point — CLOSED

**Built and validated on real data:** `datasets/detection_training/train.py`,
with `build_sourcedir.sh` and a README recording the launch parameters. It
produced `blue-plate-yolo-20260912-231818` (Completed, test mAP@50 0.995).
Note it trains at a **square** `IMGSZ` rather than the rectangular
`1088x1280` recommended in §2 — see the README's geometry contract.

What follows describes what that entry point does; the portal wiring in §4–§6
is still open. For context, the only other custom SageMaker script in the repo
is the ~40-line `_ONNX_EXPORT_SCRIPT` in
`edge-cv-portal/backend/functions/compilation.py`, which expects a TorchScript
`mochi.pt`.

The entry point, inside one training job:

1. reads the DDA ObjectDetection manifest from its input channel;
2. downloads the referenced images (`source-ref` S3 URIs);
3. builds a YOLO dataset — reuse the logic in
   `datasets/manifest_to_detector_dataset.py --format yolo`, bundling that
   module into `sourcedir.tar.gz` rather than reimplementing the parsing;
4. fine-tunes from pretrained weights at **rectangular** `imgsz=(1280, 1088)`
   with letterboxing (ultralytics' default), single class;
5. exports ONNX at exactly that input size, `opset` per `ONNX_OPSET`
   (default 17), static batch 1;
6. writes `model.onnx` to `/opt/ml/model` so it lands in the job's
   `OutputDataConfig`.

**Pattern to copy:** `compilation.py::_start_onnx_export_job` — it stages a
`sourcedir.tar.gz` to `s3://{bucket}/models/onnx-export/{job_name}/` and sets
`sagemaker_program` + `sagemaker_submit_directory` hyperparameters on a PyTorch
DLC image. Same mechanism, GPU instance, ultralytics installed.

Notes:
- ~80 epochs or fewer is plenty when fine-tuning from pretrained weights.
- 145 images is thin. Expect a usable but limited first model; the pipeline
  matters more than this particular result.
- Emit val metrics (mAP@50) into the job log so `TrainingDetail.tsx` can show
  something meaningful.

---

## 4. Gap: `edge-cv-portal/backend/functions/training.py`

Four changes. Existing LFV behavior must stay byte-identical.

**4.1 — `valid_model_types` rejects detection.** Line 328:

```python
valid_model_types = ['classification', 'segmentation',
                     'classification-robust', 'segmentation-robust']
```

Add `'object_detection'`.

**4.2 — the manifest validator rejects a bounding-box manifest.** Line 342
calls `validate_marketplace_manifest(...)` whenever
`model_source == 'marketplace'`. That function requires `anomaly-label`,
`anomaly-label-metadata`, `source-ref`. A detection manifest has
`bounding-box` / `bounding-box-metadata` instead, so it 400s with advice to
run the Manifest Transformer — which cannot help (see §5.2). Bypass it for
detection, or add a detection validator that checks
`bounding-box.annotations` and `class-map`.

**4.3 — attribute names and the algorithm are LFV-specific.** Around line 405
`attribute_names` branches only on classification vs segmentation, and the
request uses:

```python
AlgorithmSpecification={'AlgorithmName': MARKETPLACE_ALGORITHM_ARN, ...}
InputDataConfig=[{... 'S3DataType': 'AugmentedManifestFile',
                  'RecordWrapperType': 'RecordIO', 'InputMode': 'Pipe'}]
```

For detection, route to `TrainingImage` + script mode instead, and pass the
manifest as a plain `S3Prefix` / `File` channel — the entry point needs the
whole manifest to build `data.yaml`, so per-record streaming is the wrong
shape here.

**4.4 — persist the detection fields** on the `TrainingJobs` record
(`detection_arch`, network input w/h, `class_names`, thresholds) so compilation
and packaging can read them without re-deriving.

---

## 5. Gap: `edge-cv-portal/frontend/src/pages/CreateTraining.tsx`

**5.1 — no detection option.** `modelTypeOptions` (line ~66) lists only the
four LFV types. Add Object Detection. Also update the `maxRuntime` default
(line ~531) and keep the `segheadOnly` toggle hidden for detection.

**5.2 — a bounding-box manifest is misclassified as Ground Truth, which hard
blocks submission.** This is the sharpest blocker. `checkManifestFormat`
(line ~248):

```typescript
const hasGroundTruthAttrs = Object.keys(sampleEntry).some(
  key => key.endsWith('-metadata') &&
         key !== 'anomaly-label-metadata' &&
         key !== 'anomaly-mask-ref-metadata'
);
```

`bounding-box-metadata` ends with `-metadata` and is neither exclusion, so
`hasGroundTruthAttrs` is `true` → `setManifestFormat('ground-truth')`. That
then blocks submit at line 308 and adds a validation error at line 379:

> Manifest is in Ground Truth format and must be transformed before training.

And the offered remedy is a dead end: `handleTransform` (line ~288) calls
`transformManifest` with `task_type: 'segmentation' | 'classification'`, and
`edge-cv-portal/backend/layers/shared/python/manifest_transformer.py` has **no
ObjectDetection handling at all** (zero references). So the UI would insist on
a transform that cannot be performed.

Fix: recognize `bounding-box-metadata` explicitly as a third format
(`'detection'`) and skip the transform gate for it.

**5.3 — `task_type` ternary is binary.** Line 288:

```typescript
task_type: (modelType.value as string)?.includes('segmentation')
  ? 'segmentation' : 'classification',
```

Everything not segmentation becomes classification.

---

## 6. Gap: `edge-cv-portal/backend/functions/compilation.py`

A detection-trained model already has `model.onnx` from its training job, so it
must **skip SageMaker Neo entirely** and go straight to packaging. Today
`_is_onnx_import` returns `True` only when `training_job.get('source') ==
'imported'`; a detection-trained record has a different `source`, so it would
be routed into `create_compilation_job` with `Framework: 'PYTORCH'` and fail.

Either widen that predicate or have `training.py` mark detection records so the
packaging step is invoked directly.

Neo cannot emit ONNX at all — that is why the `'onnx'` pseudo-target exists as
a training job. For detection, the export already happened, so even that
pseudo-target is redundant.

---

## 7. Gap: `preserve_aspect` is not plumbed through Smart Import

`model_converter.generate_dda_package` never writes `preserve_aspect`, so any
model imported through Smart Import silently gets the squash path on device.
`BasicPreProcessor._preserve_aspect()` accepts it in the stage dict or nested
under `preprocessing` / `detection`, and `__load_model_graph_config` merges the
top-level `detection` block into every stage — so writing it into the
`detection` block is sufficient.

Measured cost of leaving it off, on 24 real frames with the deployed model
(offline, CPU ORT, preprocessing the only variable):

```
mean top confidence   squash 0.1416   letterbox 0.1918   (1.35x)
detections >= 0.08    squash 24       letterbox 41
frames with 0 dets    squash 4        letterbox 1
4608x3288 group                                          5.67x
```

The 5.67x independently reproduces the 5.02x already recorded in
`BasicPreProcessor`'s docstring, so the mechanism is well understood — it just
was never turned on for the deployed model.

Also set `network_input` correctly for a non-square input. It is only consumed
on the *non*-letterbox code path in `YoloDetectionPostProcessor._make_result`,
so it is harmless when letterboxing, but leaving it as `image_width` is
misleading.

**Train letterboxed and serve letterboxed.** A mismatch either way reintroduces
the original bug.

---

## 8. Gap: infrastructure

`compute-stack.ts` needs the detection training image URI as an env var on the
training Lambda. IAM for `sagemaker:CreateTrainingJob` already exists for the
ONNX export path. Note two pre-existing fragilities that the detection path
inherits: the hardcoded `DDASageMakerExecutionRole`, and the region-pinned
`ONNX_EXPORT_IMAGE` default in `us-east-1`.

---

## 9. Verification and deploy sequence

Everything here is cloud-side. **No LocalServer component build is needed**, so
the ~1–2h GPU compile is avoided entirely; a retrained model reaches the device
as its own Greengrass model component.

Before deploying the portal:

1. Move `edge-cv-portal/infrastructure/cdk.out` aside — the drift guard fails
   on unbaselined copies.
2. Run the preservation guards and confirm green:
   ```
   python3 -m pytest \
     test/backend-test/security/preservation/test_preservation_out_of_scope_guard.py \
     test/backend-test/security/preservation/test_preservation_secrets_out_of_scope_guard.py \
     -p no:cacheprovider --noconftest -q
   ```
3. Deploy. **Never** run a portal deploy concurrently with a component build —
   the deploy regenerates `cdk.out` and the security gate runs *after* the ~1h
   compile, so the build is wasted.

None of the files in §4–§6 are in the preservation-tracked set
(`docker-compose.yaml`, the Dockerfiles, `src/backend/requirements.txt`, the
recipe variants, `setup_station.sh`), so no baseline rebaselining is expected —
confirm rather than assume.

After deploy, the on-device change must be verified on real hardware per the
repo's edge rule: build/deploy the model component, run the workflow, confirm
detections and a healthy backend for a sustained period.

---

## 10. Traps worth knowing

- **S3 prefix collision.** `imts-plates-luggage` is a literal string prefix of
  `imts-plates-luggage-other-resolutions`, and `-` (0x2D) sorts before `/`
  (0x2F). A dataset URI without a trailing slash lists 155 objects with the 9
  parked ones *first*, so a 5-image preview shows only parked frames. This
  already caused one labeling job to run on the wrong 9 images. The wizard
  builds its prefix from free text via
  `/^s3:\/\/[^/]+\/(.+)$/` (`CreateLabelingJob.tsx:666`) with no
  normalization — adding one would be a worthwhile one-line fix.
- **Do not request TensorRT.** `OnnxRunner.__select_providers` deliberately
  excludes it for bring-your-own detection graphs: it mis-executes YOLO's
  in-graph DFL / anchor-grid ops with INT64 weights clamped to INT32, silently
  producing empty results. CUDA EP is numerically faithful.
- **No true negatives in the current label set.** All 145 images have 1–3
  boxes; zero have none. The 31 "negatives" captured were frames with plates
  *removed from the group*, not empty scenes. Good data for varied counts, but
  nothing measures false positives on blue non-plate objects. Add ~20
  background frames if FP control matters.
- **The `ryvan-cookies` bucket already holds a cookie use case.** Detection
  artifacts share the bucket with `training-images/`, `manifests/`, `labeled/`.
- **Labeling job completion 409s are benign.** The final submission flips the
  job to `Completed`; the browser's next task fetch then 409s with "Labeling
  job is not in progress". Work is already saved — verify via `submitted_count`
  and the task rows rather than trusting the banner.
