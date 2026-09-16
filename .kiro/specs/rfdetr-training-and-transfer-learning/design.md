# Design Document

## Overview

This spec extends the detection-training pipeline that `portal-detection-training` built along two axes that share almost all of their plumbing:

1. **A second detector family (RF-DETR)** — a sibling entry point selected by `detection_arch`, with the manifest converter, SageMaker launch, record shape, metric capture, compile bypass and packaging all reused. The only per-arch differences are the trainer library, the dataset layout it wants, the ONNX output contract, and the device manifest (`normalize`, `top_k`, no NMS, no letterbox).
2. **A base model for any detection run** — a `base_model` request field resolved to an S3 checkpoint URI that the entry point loads as `pretrain_weights` instead of the published COCO weights. Prior portal jobs already leave the checkpoint in their artifact (`best.pt`); imported models did not, which is why Requirement 7 was gated on the Exploration_Spike. The spike (`docs/transfer-learning-spike.md` §4) settled it: Smart Import keeps a fine-tunable `.pt`/`.pth` as a sidecar object and marks the record `metadata.fine_tunable`; everything else is classified as not fine-tunable and explained in Model Detail.

```
CreateTraining.tsx ── POST /training {model_type: object_detection,
        │                             detection_arch: yolo|rf_detr,
        │                             base_model: {kind, ref},
        │                             hyperparameters: {...}}
        ▼
training.py::_create_detection_training_job
   ├─ parse_detection_hyperparameters(arch, raw)          ← per-arch schema
   ├─ resolve_base_model(kind, ref, arch, usecase)        ← → Base_Model_Descriptor (weights_s3)
   ├─ build_sourcedir_tarball(code_dir, entry_point)      ← train.py | train_rfdetr.py (+ its requirements)
   ├─ create_training_job(sagemaker_program=<entry>, Environment={..., BASE_WEIGHTS_S3})
   └─ put_item(detection: {detection_arch, ..., base_model})
        │  (SageMaker: source-ref download → converter → fine-tune from base → ONNX → verify)
        ▼
GET /training/{id}   → metrics (unchanged regexes)
POST .../compile     → bypass (unchanged)
POST .../package     → package_trained_detection_component: arch-aware manifest + stage dir
```

## Architecture

### Entry points (`datasets/detection_training/`)

```
detection_training/
├── train.py                 # YOLO (existing) — gains BASE_WEIGHTS_S3 + uses _common
├── train_rfdetr.py          # NEW — RF-DETR
├── _common.py               # NEW — shared: env parsing, S3 helpers, source-ref download,
│                            #        converter invocation, BASE_WEIGHTS_S3 fetch, metadata writer
├── requirements.txt         # ultralytics pin (existing)
├── requirements-rfdetr.txt  # NEW — rfdetr[onnxexport]==<pin>, onnxruntime, numpy<2
├── build_sourcedir.sh       # gains --arch
└── README.md
```

`_common.py` is the third sibling in the flat sourcedir (train.py currently duplicates nothing yet; the source-ref download and manifest staging move here so RF-DETR does not copy them — Req 1.2). SageMaker extracts the tarball flat, so `train_rfdetr.py` does `from _common import ...` as a sibling import, the same way it resolves the converter.

`train_rfdetr.py`:

```python
SIZE_CLASSES = {'nano': ('RFDETRNano', 384), 'small': ('RFDETRSmall', 512),
                'medium': ('RFDETRMedium', 576), 'large': ('RFDETRLarge', 704)}

def main():
    cfg = read_env()                                  # RFDETR_SIZE, RESOLUTION, EPOCHS, BATCH, GRAD_ACCUM, LR, PATIENCE, ONNX_OPSET, BASE_WEIGHTS_S3
    if cfg.resolution % 32: sys.exit("FATAL: RESOLUTION must be a multiple of 32")
    manifest, images = stage_manifest_and_images()    # _common (source-ref or IMAGES_S3)
    dataset = run_converter(manifest, images, fmt='coco', coco_layout='rfdetr')
    assert_splits(dataset, ('train', 'valid'))
    weights = fetch_base_weights(cfg.base_weights_s3)  # None → published COCO weights
    cls_name, _native = SIZE_CLASSES[cfg.size]
    Model = getattr(rfdetr, cls_name)
    model = Model(resolution=cfg.resolution, **({'pretrain_weights': weights} if weights else {}))
    model.train(dataset_dir=dataset, epochs=..., batch_size=..., grad_accum_steps=..., lr=...,
                output_dir=WORK/'out', early_stopping=True, early_stopping_patience=cfg.patience,
                run_test=True)
    metrics = read_test_metrics(WORK/'out')           # from rfdetr's results json → TEST METRICS line
    best = WORK/'out'/'checkpoint_best_total.pth'
    export_onnx(Model, best, cfg.resolution, cfg.opset) → /opt/ml/model/model.onnx
    verify_two_outputs(model.onnx)                    # [1,Q,4] + [1,Q,C], same Q — else FATAL
    write_metadata(detection_arch='rf_detr', ...)      # _common
    shutil.copy(best, MODEL_DIR/'checkpoint_best_total.pth')
```

Facts this leans on (verified against roboflow/rf-detr docs, `develop` branch): dataset is COCO with `train/valid/test/_annotations.coco.json` and images beside the JSON; `RFDETR*(pretrain_weights=<path>)` starts from a checkpoint; `.train()` takes `dataset_dir, epochs, batch_size, grad_accum_steps, lr, output_dir, early_stopping, early_stopping_patience, run_test`; the best-model tracker writes `checkpoint_best_total.pth`; ONNX export needs the `onnxexport` extra and `model.export()`. The resolution rule is a multiple of 32 in [224, 1120] (`patch_size × num_windows = 32` for the current Nano/Small/Medium/Large checkpoints per the rfdetr FAQ; the legacy `RFDETRBase` used 56, which none of the native sizes 384/512/576/704 satisfies). The exact `export()` kwargs for a static-batch opset-17 graph are to be pinned during task 4.2 against the installed version — the spec fixes the *contract* (two outputs, static batch 1) not the call.

Geometry: RF-DETR's training transform is a square resize (not letterbox) with ImageNet mean/std. The device manifest therefore says `preserve_aspect: false, normalize: true` — the opposite of YOLO, and equally mandatory to match.

### Converter (`datasets/manifest_to_detector_dataset.py`)

`write_coco(..., layout='nested'|'rfdetr')`:
- `nested` (default): unchanged.
- `rfdetr`: split dir names `{'train': 'train', 'val': 'valid', 'test': 'test'}`; images placed at `<split>/<file_name>`; JSON at `<split>/_annotations.coco.json`. Annotation content identical.

CLI: `--coco-layout {nested,rfdetr}`. Tests extend `test_manifest_to_detector_dataset.py`.

### Shared layer (`detection_training.py`)

- `DETECTION_ARCHES = ('yolo', 'rf_detr')`; `ENTRY_POINT_FOR_ARCH = {'yolo': 'train.py', 'rf_detr': 'train_rfdetr.py'}`; `REQUIREMENTS_FOR_ARCH`.
- `parse_detection_hyperparameters(raw, arch='yolo')`: YOLO schema unchanged; RF-DETR schema `{rfdetr_size, resolution, epochs, batch, grad_accum, lr, patience, score_threshold, onnx_opset}` with the bounds in Req 3.2 and `resolution` defaulting to the size's native value.
- `detection_job_environment(manifest_s3, params, arch, base_weights_s3=None, base_weights_member=None)`: per-arch env names (RF-DETR: `MANIFEST_S3, RFDETR_SIZE, RESOLUTION, EPOCHS, BATCH, GRAD_ACCUM, LR, PATIENCE, ONNX_OPSET` — no `SCORE_THRESHOLD`, thresholds only shape the device manifest); adds `BASE_WEIGHTS_S3` when set and `BASE_WEIGHTS_MEMBER` alongside it when the URI is a tarball.
- `build_sourcedir_tarball(code_dir, out_path, entry_point)`: includes `_common.py`, the two converter files, the named entry point and *its* requirements file (`requirements.txt` ↔ `train.py`, `requirements-rfdetr.txt` ↔ `train_rfdetr.py`, renamed to `requirements.txt` inside the tar because the SageMaker toolkit installs exactly that name).
- `build_detection_device_manifest(..., detection_arch='yolo', top_k=300)`: for `rf_detr` → stage type `rf_detr_object_detection`, `normalize: True`, `preserve_aspect` defaulting to `False`, detection block with `top_k` and no `iou_threshold` (an `iou_threshold` argument is ignored for this arch). `output_shape` stays the nominal value `generate_dda_package` emits for any ONNX object-detection package (`[1, C+4, 8400]` today) rather than a hand-written `[1, 300, C]` — byte-parity with the preservation-tracked `model_converter.py` is the contract, and `packaging.py` overwrites `output_shape` with the trainer's real `onnx_output_shapes` from `training_metadata.json` anyway. Parity test extended to `detection_arch='rf_detr'` (values and key order).
- `is_trained_detection_record` unchanged.
- NEW `resolve_base_model(kind, ref, arch, usecase, training_jobs_table) -> Base_Model_Descriptor` (raises `ValueError` with a user-facing message on every Req 6.4 condition). For `training_job`: loads the record, checks `usecase_id`, `status == 'Completed'`, `detection.detection_arch == arch`, then derives the checkpoint key from `artifact_s3`: the artifact is a tarball, so the checkpoint must be *extracted once* — the entry point receives `BASE_WEIGHTS_S3` pointing at the **tarball** plus `BASE_WEIGHTS_MEMBER` (`best.pt` / `checkpoint_best_total.pth`) and extracts the member itself (no Lambda-side download of a multi-hundred-MB artifact). For `imported`: reads `metadata.fine_tunable.checkpoint_s3` (Req 7). For `published`: `{kind: 'published', ref: <name>, weights_s3: None}`.
- `classify_checkpoint(path) -> {kind, arch, fine_tunable, num_classes, class_names, evidence}` — implemented by the spike as `datasets/detection_training/_checkpoint_probe.py` (task 1.2) and promoted in task 7.1 to its own shared-layer module `edge-cv-portal/backend/layers/shared/python/checkpoint_probe.py` (≈1,150 lines of stdlib; too large to fold into this file), re-exported from `detection_training.py`. Pure and never raises: reads only the zip member set, a literal-only `pickletools.genops` walk of `data.pkl`, or the ONNX `ModelProto` skeleton (outputs + `metadata_props`, initializers skipped). Signals as measured (spike §1.4, §2): ultralytics checkpoints are torch zips whose `data.pkl` GLOBALs include `ultralytics.nn.tasks.DetectionModel` (`names`/`nc` read from the pickled model state); **RF-DETR `.pth` files carry no `rfdetr`/`lwdetr` GLOBALs** — they are identified by the state_dict key names (`class_embed.*`, `transformer.enc_out_class_embed.*`, `backbone.0.encoder.*`) plus the `args` field set (`num_queries`, `group_detr`, `encoder`, `resolution`), with `num_classes = class_embed.bias.shape[0] − 1` (never `args.num_classes`) and `class_names` from `args.class_names` when present; TorchScript zips carry `constants.pkl` + `code/`; plain `state_dict` and legacy torch tar are recognised and non-fine-tunable; ONNX starts with the protobuf header. `kind ∈ {onnx, torchscript, ultralytics_checkpoint, rfdetr_checkpoint, state_dict, legacy_torch, unknown}`; `fine_tunable` iff kind ∈ {`ultralytics_checkpoint`, `rfdetr_checkpoint`}. 25/25 on the spike set (§2.3).
- `model_converter.py` (convert handler, non-ONNX path — Req 7.1): calls `classify_checkpoint` on the downloaded source before `inspect_pytorch_model`; when fine-tunable, uploads the unmodified bytes as a sidecar `converted-models/<name>-<hex>/checkpoint.<ext>` and passes `fine_tunable = {arch, kind, checkpoint_s3, class_names, num_classes}` in the auto-import body; `model_import.py` validates that field (arch/kind vocabularies, `checkpoint_s3` inside this use case's bucket under `converted-models/`) and persists `metadata.fine_tunable` (validated dict or explicit `null`). Package generation, `inspect_pytorch_model`'s load policy and the ONNX path are untouched; the sidecar goes to the same bucket/prefix the Lambda already writes → no IAM change. `model_converter.py` is IAM-baseline-tracked → rebaseline with a note (Req 8.3).

### `training.py`

`_create_detection_training_job` gains: `detection_arch = body.get('detection_arch', 'yolo')` (400 if not in `DETECTION_ARCHES`), per-arch hyperparameter parsing, `resolve_base_model(...)`, per-arch entry point / env, and persists `detection.detection_arch`, `detection.base_model`, and for RF-DETR `top_k` instead of `iou_threshold`. The YOLO request/record shape for callers that omit `detection_arch` and `base_model` is byte-identical (pinned by the existing tests).

### `packaging.py`

`package_trained_detection_component` reads `detection_arch` (record → `training_metadata.json` → `'yolo'`), passes it to the manifest builder, nests `model.onnx` under `rf_detr_object_detection/` or `yolo_object_detection/`, and takes `resolution`/`imgsz` from the metadata as the network input.

### Frontend

- `trainingSources.ts`: `rf_detr` enabled; `byom` removed (Req 6.7); `RF_DETR_MODEL_TYPE_OPTIONS` = Object Detection; `modelTypeOptionsForSource` handles `rf_detr`; `detectionArchForSource(source) -> 'yolo'|'rf_detr'|null`.
- `CreateTraining.tsx`: Detection Settings branch per arch (YOLO: imgsz, base weights, epochs, batch, patience, score, IoU; RF-DETR: size, resolution, epochs, batch, grad accum, lr, patience, score). **Base model** `Select` under both, grouped: "Published checkpoints" / "My trained detectors (same arch)" / "Imported checkpoints". Submit adds `detection_arch` and `base_model: {kind, ref}`. Instance default `ml.g4dn.xlarge` for both; RF-DETR `medium`/`large` nudges to `ml.g5.xlarge`.
- `TrainingDetail.tsx`: "Fine-tuned from" row when `detection.base_model.kind !== 'published'`; Model Type label shows the arch.
- `ModelDetail.tsx` (Req 7.2, 7.6; task 7.4c): for `source === 'imported'`, a "Fine-tunable (YOLO|RF-DETR)" badge with `metadata.fine_tunable.class_names` (or the `num_classes` count when names are absent — published RF-DETR COCO files carry none) when `metadata.fine_tunable` is set; otherwise a per-kind explanation derived from the record (`metadata.framework`/`pt_file`): ONNX → "…Smart-Import its training checkpoint (ultralytics `.pt` or RF-DETR `.pth`); that import will appear under Base model.", TorchScript → frozen graph, state_dict → no model definition. Texts fixed in spike §4.5.
- Model Import page: **unchanged** — the provisional paired training-checkpoint upload for ONNX imports is dropped (Req 7.3 out of scope; spike §4.2). A user with both files Smart-Imports the checkpoint; that record is the base model.
- API client: `createTrainingJob` gains `detection_arch?`, `base_model?`; new `listDetectionBaseModels(usecase_id, arch)` → completed detection jobs + fine-tunable imports (a thin filter over the existing `GET /training` list and models list; no new Lambda). `FineTunableCheckpoint` type = `{arch, kind, checkpoint_s3, class_names?, num_classes?}`; `groupDetectionBaseModels` admits an import only when `fine_tunable.arch` matches and `checkpoint_s3` is set; `baseModelClassNames` falls back `fine_tunable.class_names → metadata.class_names → detection.class_names` and returns null (count-only UI, no mismatch warning) when none exist.

### Infrastructure

`compute-stack.ts`'s TrainingHandler bundler copies the extra files (`train_rfdetr.py`, `_common.py`, `requirements-rfdetr.txt`). No IAM change: base-model checkpoints live in the use-case bucket the SageMaker role already reads; the Lambda never downloads them.

### Exploration spike deliverable (`docs/transfer-learning-spike.md`)

Sections: inventory table (record → file kind → fine-tunable? → evidence), `classify_checkpoint` prototype + measured precision, the two real fine-tune runs (from `best.pt`, from `checkpoint_best_total.pth`) with mAP@50 vs baseline and wall-clock, class-head reshaping behaviour per arch, decisions for Req 7 (keep checkpoints on import? accept a paired checkpoint upload? reject TorchScript?), and the re-planned task list for 7.x.

Outcome (spike §4, adopted by task 7.0): **keep** the checkpoint on Smart Import of `.pt`/`.pth` as a sidecar + `metadata.fine_tunable`; **no** paired upload for ONNX imports (no demand, adds UI/trust surface); TorchScript, plain `state_dict`, legacy tar and ONNX **rejected** by classification with a per-kind Model Detail explanation; head re-init is left to the trainers (ultralytics unconditional; rfdetr only when `num_classes` is pinned — `train_rfdetr.py::build_model` pins it and `verify_two_outputs` is tightened to `C+1`); the YOLO `training_metadata.json` gains `num_classes`/`class_names` and ultralytics AutoUpdate is disabled before export. The `imported` branch of `resolve_base_model` and the frontend grouping were already delivered by tasks 5.1–5.3 and need no change.

The RF-DETR head-count pin matters in the entry point sketch above: on the base-weights path `Model(resolution=…, pretrain_weights=weights, trust_checkpoint=True, num_classes=len(class_names))` — without `num_classes` rfdetr 1.10.1 marks the field as user-set from the checkpoint's width and `.train()` refuses to widen it to the dataset's count.

## Data Models

`TrainingJobs.detection` gains:

```
detection_arch: 'yolo' | 'rf_detr'
top_k: 300                      # rf_detr only (replaces iou_threshold)
rfdetr_size, resolution, grad_accum, lr   # rf_detr hyperparameters used
base_model: {kind: 'published'|'training_job'|'imported', ref, weights_s3, member, detection_arch, class_names}
```

Imported records (Req 7) gain:

```
metadata.fine_tunable: {
  arch: 'yolo' | 'rf_detr',
  kind: 'ultralytics_checkpoint' | 'rfdetr_checkpoint',
  checkpoint_s3: 's3://<usecase bucket>/converted-models/<name>-<hex>/checkpoint.<pt|pth>',   # bare file, not a tarball
  class_names: string[] | null,     # ultralytics `names` / RF-DETR `args.class_names`; null for published RF-DETR COCO files
  num_classes: number               # ultralytics `nc` / RF-DETR class_embed.bias.shape[0] - 1
} | null                            # null for ONNX / TorchScript / state_dict / legacy / unknown and for every direct POST /models/import
```

`resolve_base_model('imported', …)` consumes `checkpoint_s3` as `weights_s3` with `member = None` (a bare file — the entry point's `fetch_base_weights` downloads it directly). The sidecar object is written by the converter Lambda to the same bucket/prefix as the package.

`training_metadata.json` (Req 7.4): the YOLO entry point gains `num_classes` and `class_names` (RF-DETR already writes them), so both artifacts are self-describing without opening the checkpoint.

## Error Handling

| Condition | Response |
|---|---|
| `detection_arch` not in `('yolo','rf_detr')` | 400 |
| RF-DETR `resolution` not a multiple of 32 in [224, 1120] | 400 `Invalid hyperparameter 'resolution': must be a multiple of 32 between 224 and 1120` |
| base model arch ≠ requested arch | 400 `Base model <name> is a yolo detector; cannot start an rf_detr run from it` |
| base model job not Completed / no checkpoint in artifact | 400 naming the job and the missing member |
| base model from another use case | 400 (no cross-tenant weights) |
| imported model not fine-tunable | 400 `Imported model <name> has no fine-tunable checkpoint (ONNX/TorchScript)` |
| entry point: ONNX outputs ≠ 2 or shapes mismatch | job `Failed` with `FATAL:` reason quoting shapes (surfaced in Training Detail) |

## Testing Strategy

- Converter: `rfdetr` layout cases in `test_manifest_to_detector_dataset.py`.
- Shared: RF-DETR hyperparameter schema, per-arch env, per-arch sourcedir contents, RF-DETR manifest parity with `generate_dda_package(detection_arch='rf_detr')`, `resolve_base_model` matrix, `classify_checkpoint` on the spike fixtures (small synthetic torch zips / protobuf headers checked in under `tests/fixtures/checkpoints/`).
- training.py: RF-DETR request shape; `base_model` → `BASE_WEIGHTS_S3` + `BASE_WEIGHTS_MEMBER`; YOLO preservation cases unchanged.
- packaging: RF-DETR ZIP layout + manifest; YOLO cases unchanged.
- Frontend: `trainingSources.test.ts` (rf_detr enabled, byom gone), a `CreateTraining` render test for the base-model control (first test that renders the page — needs `MemoryRouter` + `UsecaseContext` provider; pattern from `CreateLabelingJob.test.tsx`).
- Gates: preservation suite at baseline; `packaging.py` (+ `model_converter.py` if touched) rebaselined.
- Hardware: RF-DETR and fine-tuned-YOLO components verified on the JP7 DLAP (Req 8.5).
