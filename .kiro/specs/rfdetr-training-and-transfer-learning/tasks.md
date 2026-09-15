# Implementation Plan

## Notes

- Builds on `portal-detection-training` (deployed 2026-09-13). LFV and YOLO
  request/record shapes must stay byte-identical; the existing suites
  (`tests/test_detection_training_*.py`, `tests/test_detection_manifest_parity.py`)
  are the preservation pins and must pass unmodified.
- `packaging.py` is preservation-tracked (`iam_out_of_scope_baseline.json`);
  so is `model_converter.py` (touched only if Requirement 7 needs it).
  Rebaseline in the same change with a note.
- Task 1 (the spike) MUST land before tasks 7.x are implemented; tasks 7.x
  are written provisionally and are re-planned against
  `docs/transfer-learning-spike.md`. Tasks 2–6 do not depend on the spike.
- GPU work (spike runs, RF-DETR smoke) uses `ml.g4dn.xlarge`; the account
  quota is 1 concurrent instance — serialize.
- Test commands as in the parent spec: backend from `edge-cv-portal/backend`
  with `python3 -m pytest tests/<file> -q -p no:cacheprovider`; frontend
  `npx vitest run <file>` + `npm run build`; preservation suite from repo
  root with `IAM_SKIP_CDK_SYNTH=1 ... --ignore=...deserialization_roundtrip.py`.
- Never run a portal deploy while a component build is running; move
  `cdk.out` aside and run the guards first; pass
  `-c deployGroundedSamWorker=false`.

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "description": "Spike (GPU, time-boxed) in parallel with the converter layout change and shared-layer arch plumbing.", "tasks": ["1.1", "1.2", "1.3", "1.4", "2.1", "2.2", "3.1", "3.2"] },
    { "wave": 2, "description": "RF-DETR entry point + shared _common refactor; portal launch path; packaging manifest.", "tasks": ["4.1", "4.2", "4.3", "5.1", "5.2", "6.1"] },
    { "wave": 3, "description": "Frontend for RF-DETR + base model control; infra bundling.", "tasks": ["5.3", "5.4", "6.2", "6.3", "8.1"] },
    { "wave": 4, "description": "Re-plan then implement Requirement 7 from the spike findings.", "tasks": ["7.0", "7.1", "7.2", "7.3", "7.4"] },
    { "wave": 5, "description": "Gates: tests, rebaseline, preservation suite, build.", "tasks": ["9.1", "9.2", "9.3"] },
    { "wave": 6, "description": "USER ACTION: deploy, then on-device verification of RF-DETR and fine-tuned YOLO.", "tasks": ["10", "11"] }
  ]
}
```

## Tasks

- [ ] 1. Exploration spike — fine-tunable checkpoints (Requirement 5)
  - [ ] 1.1 Inventory: list every `source='imported'` record in the dev account (`aws dynamodb scan` on the training-jobs table, filter `source=imported`), download each `artifact_s3`, and classify the weights file by inspection (ONNX header / torch zip with `data.pkl` / TorchScript zip with `constants.pkl` + `code/` / RF-DETR `.pth`). Add the reference artifacts: blue-plate v2 `best.pt` (`models/training/blue-plate-*/output/model.tar.gz`), an RF-DETR `checkpoint_best_total.pth` (train one in 1.3 if none exists), a TorchScript `.pt` (any LFV `mochi.pt`), `yolo-world-blue-plate` ONNX. Record the table in `docs/transfer-learning-spike.md`.
    - _Requirements: 5.1_
  - [ ] 1.2 Prototype `classify_checkpoint(path)` as a pure function (zip/pickle envelope + protobuf header sniffing; no torch import) in `datasets/detection_training/_checkpoint_probe.py`; run it over the 1.1 set and record precision/recall per kind and the recovered `num_classes` / `class_names` where available (ultralytics stores `names` in the checkpoint; RF-DETR stores `args.class_names` or the COCO categories).
    - _Requirements: 5.2_
  - [ ] 1.3 Two real fine-tunes on the blue-plate manifest via the manual launch path (README): (a) YOLO from blue-plate v2 `best.pt` (`BASE_WEIGHTS` pointing at a downloaded copy); (b) RF-DETR small from published weights (baseline) then from its own `checkpoint_best_total.pth`. Record mAP@50, mAP@50-95, wall-clock, and whether either trainer re-initialises the class head when `nc` differs (test by fine-tuning a 1-class checkpoint onto a 2-class manifest).
    - _Requirements: 5.3, 5.4(a)_
  - [ ] 1.4 Write decisions into `docs/transfer-learning-spike.md`: keep checkpoints on Smart Import? accept a paired checkpoint upload? reject TorchScript as base? which formats are excluded and why. Re-plan tasks 7.x below against the findings (edit this file).
    - _Requirements: 5.4, 5.5_

- [ ] 2. Converter: RF-DETR COCO layout (Requirement 2)
  - [ ] 2.1 `datasets/manifest_to_detector_dataset.py`: add `--coco-layout {nested,rfdetr}`; in `rfdetr` mode place images beside `_annotations.coco.json` and name the validation split `valid`. Default unchanged.
    - _Requirements: 2.1, 2.2, 2.4_
  - [ ] 2.2 Extend `edge-cv-portal/backend/tests/test_manifest_to_detector_dataset.py`: `rfdetr` layout file placement, split names, annotation JSON identical to `nested` for the same input; existing cases untouched.
    - _Requirements: 2.3_

- [ ] 3. Shared layer: arch plumbing (Requirements 3, 4)
  - [ ] 3.1 `detection_training.py`: `DETECTION_ARCHES`, `ENTRY_POINT_FOR_ARCH`, `REQUIREMENTS_FOR_ARCH`, `RFDETR_SIZES` (native resolutions), per-arch `parse_detection_hyperparameters(raw, arch)`, per-arch `detection_job_environment(..., arch, base_weights_s3, base_weights_member)`, `build_sourcedir_tarball(code_dir, out, entry_point)` (entry point + its requirements renamed to `requirements.txt` + `_common.py` + converters), `build_detection_device_manifest(..., detection_arch, top_k)` with the RF-DETR branch (stage type, `normalize`, `[1,300,C]`, `top_k`, no `iou_threshold`).
    - _Requirements: 1.1, 3.2, 3.3, 3.4, 4.1_
  - [ ] 3.2 Tests in `tests/test_detection_training_shared.py`: RF-DETR schema (each bound, resolution multiple-of-56, size→native default), per-arch env names, per-arch tarball contents, RF-DETR manifest shape; extend `tests/test_detection_manifest_parity.py` with `detection_arch='rf_detr'` against `generate_dda_package`. YOLO cases unchanged.
    - _Requirements: 4.1, 8.1, 8.2_

- [ ] 4. RF-DETR entry point (Requirement 1)
  - [ ] 4.1 Create `datasets/detection_training/_common.py` by lifting `stage_data`'s manifest/source-ref/IMAGES_S3 download, the converter invocation, `hp()` env parsing, a `fetch_base_weights(BASE_WEIGHTS_S3, BASE_WEIGHTS_MEMBER)` (downloads the tarball and extracts the member, or downloads a bare file), and `write_metadata()`. Refactor `train.py` to use it (behaviour byte-identical; it gains base-weights loading only).
    - _Requirements: 1.2, 6.5_
  - [ ] 4.2 Create `train_rfdetr.py` per design (size map, resolution check, converter with `--coco-layout rfdetr`, split assertion, `pretrain_weights`, `.train(...)`, `TEST METRICS` line from the test split, ONNX export from `checkpoint_best_total.pth`, two-output shape verification, metadata with `detection_arch: 'rf_detr'` and RF-DETR device hints, copy of the checkpoint). Create `requirements-rfdetr.txt` (pin `rfdetr[onnxexport]`, `onnxruntime`, `numpy<2`). Pin the exact `export()` kwargs and the resolution rule against the installed version during the 1.3 baseline run.
    - _Requirements: 1.1–1.9_
  - [ ] 4.3 `build_sourcedir.sh --arch yolo|rf_detr`; README documents both entry points, the geometry contract per arch (YOLO letterbox vs RF-DETR square+normalize), and `BASE_WEIGHTS_S3`.
    - _Requirements: 1.10_

- [ ] 5. Portal launch path (Requirements 3, 6)
  - [ ] 5.1 `training.py::_create_detection_training_job`: read `detection_arch` (default `yolo`, 400 otherwise), per-arch hyperparameters, `resolve_base_model(...)` (new shared function; every Req 6.4 condition → 400 before any S3 upload), per-arch entry point and env (`BASE_WEIGHTS_S3` + `BASE_WEIGHTS_MEMBER` when set), persist `detection.detection_arch`, `detection.base_model`, RF-DETR fields (`rfdetr_size`, `resolution`, `grad_accum`, `lr`, `top_k`; no `iou_threshold`).
    - _Requirements: 3.2, 3.3, 6.3, 6.4, 6.5, 6.6_
  - [ ] 5.2 Tests in `tests/test_detection_training_create.py`: RF-DETR request shape (`sagemaker_program='train_rfdetr.py'`, env has `RFDETR_SIZE`/`RESOLUTION`/`GRAD_ACCUM`/`LR`, no `IOU`), resolution 500 → 400, base model from a completed YOLO job → env carries `BASE_WEIGHTS_S3` = that job's `artifact_s3` and `BASE_WEIGHTS_MEMBER='best.pt'`, arch mismatch → 400, InProgress base → 400, other use case → 400, published → no `BASE_WEIGHTS_S3`; YOLO and LFV cases unchanged.
    - _Requirements: 8.2_
  - [ ] 5.3 Frontend `trainingSources.ts`: enable `rf_detr`, remove `byom`, add `detectionArchForSource`; update `trainingSources.test.ts`. `CreateTraining.tsx`: per-arch Detection Settings, **Base model** Select (published / my detectors same arch / fine-tunable imports) fed by `listDetectionBaseModels`, class-name prefill + mismatch warning, submit `detection_arch` + `base_model`, instance default nudges. `TrainingDetail.tsx`: "Fine-tuned from" row + arch in Model Type. `api.ts` types.
    - _Requirements: 3.1, 3.5, 3.6, 6.1, 6.2, 6.7_
  - [ ] 5.4 First `CreateTraining` render test (`src/pages/CreateTraining.baseModel.test.tsx`, MemoryRouter + UsecaseContext provider, `apiService` mocked): RF-DETR source shows RF-DETR settings and no IoU; base-model list groups correctly; submit payload carries `detection_arch` and `base_model`.
    - _Requirements: 8.2_

- [ ] 6. Packaging (Requirement 4)
  - [ ] 6.1 `packaging.package_trained_detection_component`: arch from record → metadata → `'yolo'`; RF-DETR stage dir `rf_detr_object_detection/`; network input from `training_metadata.json` `resolution`/`imgsz`; pass `detection_arch`/`top_k` to the manifest builder. Tests in `tests/test_detection_training_compile_package.py` for an RF-DETR artifact (two-output metadata, `top_k`, `normalize: true`, `preserve_aspect: false`, stage dir); YOLO cases unchanged.
    - _Requirements: 4.1, 4.2, 4.3_
  - [ ] 6.2 Rebaseline `packaging.py` sha256 in `iam_out_of_scope_baseline.json` with a note.
    - _Requirements: 8.3_
  - [ ] 6.3 Update `docs/detection-training-gap.md` status paragraph to record RF-DETR support and link this spec.
    - _Requirements: —_

- [ ] 7. Imported models as base models (Requirement 7 — PROVISIONAL; re-plan in 7.0 from the spike)
  - [ ] 7.0 Re-plan: rewrite 7.1–7.4 below from `docs/transfer-learning-spike.md` (which formats are in, which are excluded, whether Smart Import keeps the checkpoint, whether a paired upload is added). Update requirements.md Req 7 accordingly. Do not implement 7.1–7.4 before this.
    - _Requirements: 5.5_
  - [ ] 7.1 Move `classify_checkpoint` from the spike prototype into `detection_training.py`; fixtures under `tests/fixtures/checkpoints/`; tests for every kind in the inventory.
    - _Requirements: 7.1, 7.2, 8.2_
  - [ ] 7.2 `model_converter.py` / `model_import.py`: on `.pt`/`.pth` import, classify, copy the original checkpoint to `converted-models/<name>-<hex>/checkpoint.<ext>` in the use-case bucket, persist `metadata.fine_tunable = {arch, kind, checkpoint_s3}` (or `null`). Rebaseline `model_converter.py` in the IAM guard.
    - _Requirements: 7.1, 7.2, 8.3_
  - [ ] 7.3 Model Import page: optional paired training-checkpoint upload for ONNX imports; `ModelDetail.tsx` fine-tunable badge / explanation.
    - _Requirements: 7.2, 7.3_
  - [ ] 7.4 Entry points: class-head re-init when the manifest's class set differs from the base checkpoint's (per arch, as the spike found), logging old/new class lists; `resolve_base_model('imported', ...)` reads `metadata.fine_tunable.checkpoint_s3`.
    - _Requirements: 7.4, 7.5, 6.3_

- [ ] 8. Infrastructure
  - [ ] 8.1 `compute-stack.ts`: add `train_rfdetr.py`, `_common.py`, `requirements-rfdetr.txt` to the TrainingHandler bundle list (local bundler + Docker fallback). No IAM change. `npm run build` in `infrastructure/`.
    - _Requirements: 3.4, 8.4_

- [ ] 9. Gates
  - [ ] 9.1 Backend: all `tests/test_detection_*`, `tests/test_manifest_to_detector_dataset.py`, `tests/test_onnx_jetson_*`, `tests/test_vision_model_packaging_*`, `tests/test_property_packaging_gates.py`, `tests/test_model_converter_preserve_aspect.py` green.
    - _Requirements: 8.1, 8.2_
  - [ ] 9.2 Preservation suite at or better than baseline (`IAM_SKIP_CDK_SYNTH=1`, roundtrip module ignored); `packaging.py` / `model_converter.py` rebaselined; `test_preservation_secrets_packaging.py` still injects `detection_training`.
    - _Requirements: 8.3, 8.4_
  - [ ] 9.3 Frontend: `npx vitest run` for `trainingSources`, `manifestFormat`, `CompilationTab.detection`, `CreateTraining.baseModel`; `npm run build`.
    - _Requirements: 8.2_

- [ ] 10. USER ACTION — portal deploy
  - No component build running; `cdk.out` aside; guards green; infra + frontend deploy with `-c deployGroundedSamWorker=false`; verify `TrainingHandler` bundle contains `train_rfdetr.py` and `_common.py`.
    - _Requirements: 8.4_

- [ ] 11. USER ACTION — on-device verification
  - (a) RF-DETR small on the blue-plate manifest → Package → Publish → deploy to the JP7 DLAP → workflow detects with a healthy backend for a sustained period; confirm the device manifest has `normalize: true`, `preserve_aspect: false`, `top_k: 300`. (b) YOLO fine-tuned from blue-plate v2 `best.pt` with the same manifest → same path; compare mAP@50 and on-device confidence against v2. Record both in `docs/detection-training-gap.md`.
    - _Requirements: 8.5_
