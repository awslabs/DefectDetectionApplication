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

- [x] 1. Exploration spike — fine-tunable checkpoints (Requirement 5)
  - [x] 1.1 Inventory: list every `source='imported'` record in the dev account (`aws dynamodb scan` on the training-jobs table, filter `source=imported`), download each `artifact_s3`, and classify the weights file by inspection (ONNX header / torch zip with `data.pkl` / TorchScript zip with `constants.pkl` + `code/` / RF-DETR `.pth`). Add the reference artifacts: blue-plate v2 `best.pt` (`models/training/blue-plate-*/output/model.tar.gz`), an RF-DETR `checkpoint_best_total.pth` (train one in 1.3 if none exists), a TorchScript `.pt` (any LFV `mochi.pt`), `yolo-world-blue-plate` ONNX. Record the table in `docs/transfer-learning-spike.md`.
    - _Requirements: 5.1_
  - [x] 1.2 Prototype `classify_checkpoint(path)` as a pure function (zip/pickle envelope + protobuf header sniffing; no torch import) in `datasets/detection_training/_checkpoint_probe.py`; run it over the 1.1 set and record precision/recall per kind and the recovered `num_classes` / `class_names` where available (ultralytics stores `names` in the checkpoint; RF-DETR stores `args.class_names` or the COCO categories).
    - _Requirements: 5.2_
  - [x] 1.3 Two real fine-tunes on the blue-plate manifest via the manual launch path (README): (a) YOLO from blue-plate v2 `best.pt` (`BASE_WEIGHTS` pointing at a downloaded copy); (b) RF-DETR small from published weights (baseline) then from its own `checkpoint_best_total.pth`. Record mAP@50, mAP@50-95, wall-clock, and whether either trainer re-initialises the class head when `nc` differs (test by fine-tuning a 1-class checkpoint onto a 2-class manifest).
    - _Requirements: 5.3, 5.4(a)_
  - [x] 1.4 Write decisions into `docs/transfer-learning-spike.md`: keep checkpoints on Smart Import? accept a paired checkpoint upload? reject TorchScript as base? which formats are excluded and why. Re-plan tasks 7.x below against the findings (edit this file).
    - _Requirements: 5.4, 5.5_

- [x] 2. Converter: RF-DETR COCO layout (Requirement 2)
  - [x] 2.1 `datasets/manifest_to_detector_dataset.py`: add `--coco-layout {nested,rfdetr}`; in `rfdetr` mode place images beside `_annotations.coco.json` and name the validation split `valid`. Default unchanged.
    - _Requirements: 2.1, 2.2, 2.4_
  - [x] 2.2 Extend `edge-cv-portal/backend/tests/test_manifest_to_detector_dataset.py`: `rfdetr` layout file placement, split names, annotation JSON identical to `nested` for the same input; existing cases untouched.
    - _Requirements: 2.3_

- [x] 3. Shared layer: arch plumbing (Requirements 3, 4)
  - [x] 3.1 `detection_training.py`: `DETECTION_ARCHES`, `ENTRY_POINT_FOR_ARCH`, `REQUIREMENTS_FOR_ARCH`, `RFDETR_SIZES` (native resolutions), per-arch `parse_detection_hyperparameters(raw, arch)`, per-arch `detection_job_environment(..., arch, base_weights_s3, base_weights_member)`, `build_sourcedir_tarball(code_dir, out, entry_point)` (entry point + its requirements renamed to `requirements.txt` + `_common.py` + converters), `build_detection_device_manifest(..., detection_arch, top_k)` with the RF-DETR branch (stage type, `normalize`, `[1,300,C]`, `top_k`, no `iou_threshold`).
    - _Requirements: 1.1, 3.2, 3.3, 3.4, 4.1_
  - [x] 3.2 Tests in `tests/test_detection_training_shared.py`: RF-DETR schema (each bound, resolution multiple-of-56, size→native default), per-arch env names, per-arch tarball contents, RF-DETR manifest shape; extend `tests/test_detection_manifest_parity.py` with `detection_arch='rf_detr'` against `generate_dda_package`. YOLO cases unchanged.
    - _Requirements: 4.1, 8.1, 8.2_

- [x] 4. RF-DETR entry point (Requirement 1)
  - [x] 4.1 Create `datasets/detection_training/_common.py` by lifting `stage_data`'s manifest/source-ref/IMAGES_S3 download, the converter invocation, `hp()` env parsing, a `fetch_base_weights(BASE_WEIGHTS_S3, BASE_WEIGHTS_MEMBER)` (downloads the tarball and extracts the member, or downloads a bare file), and `write_metadata()`. Refactor `train.py` to use it (behaviour byte-identical; it gains base-weights loading only).
    - _Requirements: 1.2, 6.5_
  - [x] 4.2 Create `train_rfdetr.py` per design (size map, resolution check, converter with `--coco-layout rfdetr`, split assertion, `pretrain_weights`, `.train(...)`, `TEST METRICS` line from the test split, ONNX export from `checkpoint_best_total.pth`, two-output shape verification, metadata with `detection_arch: 'rf_detr'` and RF-DETR device hints, copy of the checkpoint). Create `requirements-rfdetr.txt` (pin `rfdetr[onnxexport]`, `onnxruntime`, `numpy<2`). Pin the exact `export()` kwargs and the resolution rule against the installed version during the 1.3 baseline run.
    - _Requirements: 1.1–1.9_
  - [x] 4.3 `build_sourcedir.sh --arch yolo|rf_detr`; README documents both entry points, the geometry contract per arch (YOLO letterbox vs RF-DETR square+normalize), and `BASE_WEIGHTS_S3`.
    - _Requirements: 1.10_

- [x] 5. Portal launch path (Requirements 3, 6)
  - [x] 5.1 `training.py::_create_detection_training_job`: read `detection_arch` (default `yolo`, 400 otherwise), per-arch hyperparameters, `resolve_base_model(...)` (new shared function; every Req 6.4 condition → 400 before any S3 upload), per-arch entry point and env (`BASE_WEIGHTS_S3` + `BASE_WEIGHTS_MEMBER` when set), persist `detection.detection_arch`, `detection.base_model`, RF-DETR fields (`rfdetr_size`, `resolution`, `grad_accum`, `lr`, `top_k`; no `iou_threshold`).
    - _Requirements: 3.2, 3.3, 6.3, 6.4, 6.5, 6.6_
  - [x] 5.2 Tests in `tests/test_detection_training_create.py`: RF-DETR request shape (`sagemaker_program='train_rfdetr.py'`, env has `RFDETR_SIZE`/`RESOLUTION`/`GRAD_ACCUM`/`LR`, no `IOU`), resolution 500 → 400, base model from a completed YOLO job → env carries `BASE_WEIGHTS_S3` = that job's `artifact_s3` and `BASE_WEIGHTS_MEMBER='best.pt'`, arch mismatch → 400, InProgress base → 400, other use case → 400, published → no `BASE_WEIGHTS_S3`; YOLO and LFV cases unchanged.
    - _Requirements: 8.2_
  - [x] 5.3 Frontend `trainingSources.ts`: enable `rf_detr`, remove `byom`, add `detectionArchForSource`; update `trainingSources.test.ts`. `CreateTraining.tsx`: per-arch Detection Settings, **Base model** Select (published / my detectors same arch / fine-tunable imports) fed by `listDetectionBaseModels`, class-name prefill + mismatch warning, submit `detection_arch` + `base_model`, instance default nudges. `TrainingDetail.tsx`: "Fine-tuned from" row + arch in Model Type. `api.ts` types.
    - _Requirements: 3.1, 3.5, 3.6, 6.1, 6.2, 6.7_
  - [x] 5.4 First `CreateTraining` render test (`src/pages/CreateTraining.baseModel.test.tsx`, MemoryRouter + UsecaseContext provider, `apiService` mocked): RF-DETR source shows RF-DETR settings and no IoU; base-model list groups correctly; submit payload carries `detection_arch` and `base_model`.
    - _Requirements: 8.2_

- [x] 6. Packaging (Requirement 4)
  - [x] 6.1 `packaging.package_trained_detection_component`: arch from record → metadata → `'yolo'`; RF-DETR stage dir `rf_detr_object_detection/`; network input from `training_metadata.json` `resolution`/`imgsz`; pass `detection_arch`/`top_k` to the manifest builder. Tests in `tests/test_detection_training_compile_package.py` for an RF-DETR artifact (two-output metadata, `top_k`, `normalize: true`, `preserve_aspect: false`, stage dir); YOLO cases unchanged.
    - _Requirements: 4.1, 4.2, 4.3_
  - [x] 6.2 Rebaseline `packaging.py` sha256 in `iam_out_of_scope_baseline.json` with a note.
    - _Requirements: 8.3_
  - [x] 6.3 Update `docs/detection-training-gap.md` status paragraph to record RF-DETR support and link this spec.
    - _Requirements: —_

- [x] 7. Imported models as base models (Requirement 7 — re-planned in 7.0 from `docs/transfer-learning-spike.md` §4–§5; ultralytics `.pt` and RF-DETR `.pth` are in, TorchScript / state_dict / legacy tar / ONNX are out, Smart Import keeps the checkpoint, no paired upload)
  - [x] 7.0 Re-plan: rewrite 7.1–7.4 below from `docs/transfer-learning-spike.md` (which formats are in, which are excluded, whether Smart Import keeps the checkpoint, whether a paired upload is added). Update requirements.md Req 7 accordingly. Do not implement 7.1–7.4 before this.
    - _Requirements: 5.5_
  - [x] 7.1 Promote the probe: move `datasets/detection_training/_checkpoint_probe.py` to `edge-cv-portal/backend/layers/shared/python/checkpoint_probe.py` unchanged in behaviour (stdlib only, never raises, `KINDS`/`ARCHES` as in spike §2.1); re-export `classify_checkpoint` from `detection_training.py`; delete the prototype. Fixtures under `edge-cv-portal/backend/tests/fixtures/checkpoints/` built synthetically at test time (small torch-zip envelopes written with `zipfile` + hand-assembled `data.pkl` opcodes: ultralytics `names`/`nc`, RF-DETR published Namespace layout, RF-DETR 1.10.1 dict layout with `args.class_names`, TorchScript `constants.pkl`+`code/`, plain `state_dict`, legacy tar, ONNX header with/without `metadata_props.names`, ELF/garbage/0-byte/truncated). Tests in `tests/test_checkpoint_probe.py`: every kind → expected `{kind, arch, fine_tunable, num_classes, class_names}`; `fine_tunable` true iff kind ∈ {`ultralytics_checkpoint`, `rfdetr_checkpoint`}; RF-DETR `num_classes` from `class_embed.bias` − 1 and never from `args.num_classes`; no exception on any garbage input.
    - _Requirements: 7.1, 7.2, 7.5, 8.2_
  - [x] 7.2 Smart Import keeps the checkpoint. `edge-cv-portal/backend/functions/model_converter.py` (convert handler, `export_format != 'onnx'` path only): after download and before `inspect_pytorch_model`, `probe = classify_checkpoint(local_model)`; when `probe['fine_tunable']`, `upload_file` the **unmodified** source to `converted-models/<safe_model_name>-<hex>/checkpoint.<source ext>` (same `<hex>` as the package key) in the use-case bucket and set `fine_tunable = {arch, kind, checkpoint_s3, class_names, num_classes}`, else `fine_tunable = None`; include `fine_tunable` in the auto-import body and in the 200 response; leave `inspect_pytorch_model`, `generate_dda_package` and the ONNX path byte-identical. `edge-cv-portal/backend/functions/model_import.py`: accept optional `fine_tunable` in the import body, validate it (`arch ∈ {yolo, rf_detr}`, `kind ∈ {ultralytics_checkpoint, rfdetr_checkpoint}`, `checkpoint_s3` an `s3://<this use case's bucket>/converted-models/…` URI, `class_names` a list of strings or null) → 400 on anything else, and persist `metadata.fine_tunable` (validated dict or explicit `null`; `null` when the field is absent). Tests: `tests/test_model_converter_fine_tunable.py` (moto: ultralytics fixture → sidecar object exists with identical bytes + `fine_tunable` in the import payload; TorchScript/state_dict fixture → no sidecar, `fine_tunable: null`; ONNX path never calls the probe) and `tests/test_model_import_fine_tunable.py` (persisted shape; absent → `null`; wrong bucket/arch/kind → 400). Rebaseline `model_converter.py` sha256 in `test/backend-test/security/baselines/iam_out_of_scope_baseline.json` with a note naming this task.
    - _Requirements: 7.1, 7.2, 7.5, 8.3_
  - [x] 7.3 DROPPED — no paired training-checkpoint upload for ONNX imports: zero demand (7/7 imports ONNX-only, no pairable checkpoint in the account) and it adds UI + trust + record-update surface; the supported path is to Smart-Import the checkpoint itself (spike §4.2). `ModelDetail.tsx` work moved to 7.4.
    - _Requirements: 7.3 (out of scope)_
  - [x] 7.4 Entry points + Model Detail. (a) `datasets/detection_training/train_rfdetr.py`: keep the `build_model(..., num_classes=len(class_names))` pin on the base-weights path (validated on SageMaker `tl13-rfdetr-nc2b-0625`); tighten `verify_two_outputs` to exactly `[1, Q, C+1]` (a `C`-slot graph is `FATAL`, message quotes both shapes); update `tests/test_train_rfdetr_static.py` (`C` slots → raises, `C+1` → ok, existing `build_model` pin tests stay). (b) `datasets/detection_training/train.py`: write `num_classes` and `class_names` into `training_metadata.json` (from the converter's `data.yaml` names); `from ultralytics import settings; settings.update(autoinstall=False)` before `model.export(...)`; new static suite `tests/test_train_yolo_static.py` (pattern of `test_train_rfdetr_static.py`: import the module with `ultralytics` stubbed) asserting the metadata dict carries `num_classes`/`class_names` and that `settings.update(autoinstall=False)` runs before `model.export`. Both entry points keep their old-vs-new class log lines; neither re-initialises a head itself. (c) `edge-cv-portal/frontend/src/pages/ModelDetail.tsx`: for `source === 'imported'`, render a "Fine-tunable (YOLO|RF-DETR)" badge with class names (or count when names are absent) when `metadata.fine_tunable` is set, otherwise the per-kind explanation from spike §4.5 (ONNX → "Smart-Import its training checkpoint (.pt/.pth)…", TorchScript → frozen graph, state_dict → no model definition); test `ModelDetail.fineTunable.test.tsx` covering the three texts and the badge. No `resolve_base_model` change (the `imported` branch and its 8 tests already exist).
    - _Requirements: 7.2, 7.4, 7.5, 6.3, 8.2_

- [x] 8. Infrastructure
  - [x] 8.1 `compute-stack.ts`: add `train_rfdetr.py`, `_common.py`, `requirements-rfdetr.txt` to the TrainingHandler bundle list (local bundler + Docker fallback). No IAM change. `npm run build` in `infrastructure/`.
    - _Requirements: 3.4, 8.4_

- [x] 9. Gates
  - [x] 9.1 Backend: all `tests/test_detection_*`, `tests/test_manifest_to_detector_dataset.py`, `tests/test_onnx_jetson_*`, `tests/test_vision_model_packaging_*`, `tests/test_property_packaging_gates.py`, `tests/test_model_converter_preserve_aspect.py` green.
    - _Requirements: 8.1, 8.2_
  - [x] 9.2 Preservation suite at or better than baseline (`IAM_SKIP_CDK_SYNTH=1`, roundtrip module ignored); `packaging.py` / `model_converter.py` rebaselined; `test_preservation_secrets_packaging.py` still injects `detection_training`.
    - Verified 2026-09-22 on a clean worktree of `f425ed2` (the deployed tree): host 140 passed / 7 skipped; flask-app container 132 passed / 8 skipped (skips = torch/jwt/CDK unavailable + no `cdk.out`, all pre-existing). `packaging.py` / `model_converter.py` sha256 match `iam_out_of_scope_baseline.json`.
    - _Requirements: 8.3, 8.4_
  - [x] 9.3 Frontend: `npx vitest run` for `trainingSources`, `manifestFormat`, `CompilationTab.detection`, `CreateTraining.baseModel`; `npm run build`.
    - _Requirements: 8.2_

- [x] 10. USER ACTION — portal deploy
  - No component build running; `cdk.out` aside; guards green; infra + frontend deploy with `-c deployGroundedSamWorker=false`; verify `TrainingHandler` bundle contains `train_rfdetr.py` and `_common.py`.
    - Deployed 2026-09-18 15:02 UTC (account 164152369890, alongside the quality-prompt-tuning deploy). Verified 2026-09-22: `TrainingHandler` bundle has `detection_training/train_rfdetr.py`, `_common.py`, `requirements-rfdetr.txt`; SharedLayer v80 has `checkpoint_probe.py`; Packaging / ModelConverter / ModelImport handlers updated the same minute; frontend `dda-portal-frontend-164152369890` main bundle carries `rf_detr`, "Fine-tuned from", "Fine-tunable". No RF-DETR job has been launched through the portal yet (training-jobs table holds only the two YOLO runs).
    - _Requirements: 8.4_

- [x] 11. USER ACTION — on-device verification
  - (a) RF-DETR small on the blue-plate manifest → Package → Publish → deploy to the JP7 DLAP → workflow detects with a healthy backend for a sustained period; confirm the device manifest has `normalize: true`, `preserve_aspect: false`, `top_k: 300`. (b) YOLO fine-tuned from blue-plate v2 `best.pt` with the same manifest → same path; compare mAP@50 and on-device confidence against v2. Record both in `docs/detection-training-gap.md`.
    - **DONE 2026-09-22 on `jetson-thor1`** (JP7 Orin, LocalServer `arm64JP7` **1.0.43**) instead of the DLAP, which was offline (MQTT keep-alive timeout since 2026-09-19). Both legs driven entirely through the deployed portal API. Full record in `docs/detection-training-gap.md` §"On-device verification (2026-09-22)".
    - (a) RF-DETR: portal job `be389241` / `blue-plate-rfdetr-small-20260922-145600`, 1134 s, **test mAP@50 1.0, mAP@50-95 0.958**. Device manifest confirmed `rf_detr_object_detection` stage, `normalize: true`, `preserve_aspect: false`, `top_k: 300`, `output_shapes [[1,300,4],[1,300,2]]`, no `iou_threshold`. Component `model-blue-plate-rfdetr-small-jetson-xavier-jp7` 1.0.0. **12 workflow runs, 11 OK**, every one returning exactly 3 `blue_plate` boxes at 0.951 / 0.940 / 0.939 within ~10–45 px of the labeled ground truth in the original 2001×2352 space — proof the device denormalises `[0,1]` DETR output with squash geometry correctly. Backend healthy across ~30 min and 11 runs with no restart.
    - (b) YOLO fine-tune: portal job `99f9c131` / `blue-plate-yolo-ft-v3-20260922-172408` from `imts-blue-plates-detector-v3` (`base_model.kind='training_job'` → `BASE_WEIGHTS_S3` = v3's artifact + `BASE_WEIGHTS_MEMBER='best.pt'`, echoed into `training_metadata.json`), 627 s, **test mAP@50 0.995 / mAP@50-95 0.8564 / P 0.998 / R 1.0 vs the v3 base's 0.9918 / 0.8201 / 0.9804 / 0.9710**. Device manifest is the YOLO contract (`yolo_object_detection`, `preserve_aspect: true`, `iou_threshold: 0.45`, `normalize: false`) — the per-arch packaging branch proven byte-level against the RF-DETR one. On-device on the identical frame: fine-tuned **0.769 / 0.932 / 0.915 (mean 0.872)** vs v3 base **0.789 / 0.817 / 0.835 (mean 0.814)**; RF-DETR highest and most uniform at mean 0.943. All three found 3/3 plates.
    - Two pre-existing device bugs surfaced (neither RF-DETR-specific, both recorded in the gap doc): the workflow engine's camera inventory omits the Static_Image_Camera, and the first workflow run after a backend restart loses a Triton readiness race.
    - _Requirements: 8.5_
