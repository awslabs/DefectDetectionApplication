# Implementation Plan

## Notes

- LFV (classification/segmentation) behaviour must stay byte-identical: every
  detection change is an early branch or an additive block. Task 2.2 pins the
  LFV `create_training_job` request shape before the detection branch lands.
- `packaging.py` is preservation-tracked (`iam_out_of_scope_baseline.json` →
  `sibling_spec_files`). Rebaseline its sha256 in the same change (task 7.1).
  `model_converter.py` is also tracked and is NOT edited by this spec.
- No new IAM actions. If one surfaces, stop and follow the security gate's
  reviewed rebaseline protocol instead of widening a grant.
- Test commands:
  - Backend (from `edge-cv-portal/backend`, WITH conftest):
    `python3 -m pytest tests/<file> -q -p no:cacheprovider`
  - Frontend (from `edge-cv-portal/frontend`): `npx vitest run <file>`,
    `npm run build`
  - Preservation (repo root):
    `python3 -m pytest test/backend-test/security/preservation -q -p no:cacheprovider --noconftest --ignore=test/backend-test/security/preservation/test_preservation_deserialization_roundtrip.py`
- The WSL shell tool drops output intermittently; write command output to a
  file under `tmp/` and read it back.

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "description": "Shared module + entry-point change + LFV preservation test (independent).", "tasks": ["1.1", "1.2", "2.1", "2.2"] },
    { "wave": 2, "description": "Backend Lambdas: training create/get, compile bypass, package.", "tasks": ["3.1", "3.2", "4.1", "4.2"] },
    { "wave": 3, "description": "Backend tests for the detection path + parity.", "tasks": ["5.1", "5.2", "5.3"] },
    { "wave": 4, "description": "Frontend + infra.", "tasks": ["6.1", "6.2", "6.3", "6.4", "6.5", "8.1"] },
    { "wave": 5, "description": "Gates: rebaseline packaging.py, run preservation suite, frontend build.", "tasks": ["7.1", "7.2", "7.3"] },
    { "wave": 6, "description": "USER ACTION: portal deploy, then on-device verification.", "tasks": ["9", "10"] }
  ]
}
```

## Tasks

- [x] 1. Shared detection helpers
  - [x] 1.1 Create `edge-cv-portal/backend/layers/shared/python/detection_training.py` with `MODEL_TYPE_OBJECT_DETECTION`, `DETECTION_STAGE_TYPE`, `DETECTION_DEFAULTS`, `DETECTION_METRIC_DEFINITIONS`, `is_trained_detection_record`, `resolve_detection_training_image`, `parse_detection_hyperparameters`, `detect_bbox_attribute`, `validate_detection_manifest_entry`, `build_detection_device_manifest`, `build_sourcedir_tarball` (design §Architecture).
    - _Requirements: 2.2, 2.3, 2.6, 3.2, 3.7, 3.8, 3.9, 4.4, 5.3_
  - [x] 1.2 Write `edge-cv-portal/backend/tests/test_detection_training_shared.py` covering every helper (defaults, each hyperparameter violation, DDA/GT/classification entries, image resolution, tarball layout) plus a Hypothesis round-trip property for valid hyperparameters.
    - _Requirements: 2.2, 2.3, 2.4, 2.6, 3.8, 3.9, 9.2_

- [x] 2. Entry point and LFV preservation
  - [x] 2.1 Update `datasets/detection_training/train.py::stage_data()` so `IMAGES_S3` is optional: without it, download each manifest `source-ref`; warn on basename collisions; `main()` requires only `MANIFEST_S3`. Update the README's launch snippet note and close its "Portal integration" section.
    - _Requirements: 3.4, 3.5_
  - [x] 2.2 Write `edge-cv-portal/backend/tests/test_detection_training_create.py` with the LFV preservation case first: a classification request through `training.create_training_job` (moto + `FakeSageMakerService`) yields the exact current `create_training_job` kwargs (`AlgorithmName`, `AugmentedManifestFile`, `RecordIO`, `Pipe`, `EnableNetworkIsolation=True`) and the current DynamoDB item keys. Run it green on the unchanged tree.
    - _Requirements: 1.2, 9.2_

- [x] 3. `training.py`
  - [x] 3.1 Add `'object_detection'` to `valid_model_types`; add `validate_detection_manifest(manifest_uri, usecase)` and `_create_detection_training_job(...)` implementing design steps 1–9 (hyperparameter parsing → manifest validation → naming → sourcedir build/upload → image resolution → `create_training_job` with `TrainingImage`/script mode/`MetricDefinitions`/`InputDataConfig=[]`/`EnableNetworkIsolation=False`/60 GB → `put_item` with `runtime`, `algorithm_uri`, `detection`). Branch to it right after the access check so the LFV code below is untouched. Defaults: `ml.g4dn.xlarge`, `10800`.
    - _Requirements: 1.1, 1.3, 1.4, 2.1, 2.4, 2.5, 3.1, 3.2, 3.3, 3.4, 3.6, 3.7, 3.8, 3.9, 3.10, 4.1, 4.4_
  - [x] 3.2 In `get_training_job`, after `describe_training_job`, hydrate `metrics` from `FinalMetricDataList` for detection records that are `Completed` and have no `metrics` (independent of the status-changed block). LFV records untouched.
    - _Requirements: 4.2, 4.3_

- [x] 4. Compile bypass and packaging
  - [x] 4.1 `compilation.py`: import `is_trained_detection_record`; widen the bypass condition to `_is_onnx_import(job) or is_trained_detection_record(job)`; distinguish the log line only.
    - _Requirements: 5.1, 5.8_
  - [x] 4.2 `packaging.py`: add `package_trained_detection_component(trained_model_s3, training_job, s3_client, usecase)` (design §packaging.py) and a `package_components` branch after the vLLM bypass and before the ONNX-import bypass that mirrors the import bypass shape (targets default, `packaged_components`, audit, `_trigger_component_creation`). Leave the import block textually unchanged.
    - _Requirements: 5.2, 5.3, 5.4, 5.5, 5.6, 5.8_

- [x] 5. Backend tests for the detection path
  - [x] 5.1 Extend `tests/test_detection_training_create.py`: detection request → `TrainingImage`, script-mode `HyperParameters` + `Environment`, `InputDataConfig=[]`, `EnableNetworkIsolation=False`, `MetricDefinitions`, sourcedir object present in the use-case bucket, record has `runtime='onnx'` + `detection` block; classification manifest → 400 without Transformer suggestion; bad `imgsz` → 400 and zero SageMaker calls; `get_training_job` hydrates `metrics`. Stage the four entry-point files into a temp dir and point `DETECTION_TRAINING_CODE_DIR` at it.
    - _Requirements: 2.1, 2.4, 3.1, 3.3, 3.4, 3.6, 3.7, 3.8, 4.1, 4.2, 9.2_
  - [x] 5.2 Write `tests/test_detection_training_compile_package.py`: compile bypass for a detection record (no SageMaker calls, `compilation_skipped`, 200); package builds a ZIP with `manifest.json` (Req 5.3 content, `preserve_aspect` true, `class_names`) and `yolo_object_detection/model.onnx`, one entry per default target; `training_metadata.json` `imgsz` wins over the record; artifact without `.onnx` → 500 and no `packaged_components`; an imported-ONNX record still takes `package_onnx_component`.
    - _Requirements: 5.1, 5.2, 5.3, 5.4, 5.5, 5.6, 5.8, 9.2_
  - [x] 5.3 Write `tests/test_detection_manifest_parity.py`: for several `(imgsz, class_names, thresholds)` sets, `build_detection_device_manifest` equals `model_converter.generate_dda_package(export_format='onnx', model_type='object_detection', preserve_aspect=True)`'s `export_artifacts/manifest.json` after removing `dataset`.
    - _Requirements: 5.7, 9.2_

- [x] 6. Frontend
  - [x] 6.1 Create `src/utils/manifestFormat.ts` (`classifyManifestFormat`) and `src/utils/manifestFormat.test.ts`.
    - _Requirements: 6.2, 9.3_
  - [x] 6.2 `CreateTraining.tsx`: add the Object Detection option; `'detection'` manifest format via the classifier; `isDetection` branches for instance list (g4dn.xlarge default), max runtime `10800`, hidden seghead/robust UI, Detection Settings container with the seven inputs, submit `hyperparameters`, the two mismatch validation errors, detection success alert, and detection-specific guidance text. LFV rendering unchanged.
    - _Requirements: 6.1, 6.3, 6.4, 6.5, 6.6, 6.7, 6.8, 6.9, 6.10_
  - [x] 6.3 `CompilationTab.tsx`: extend `isOnnxModel` with `runtime === 'onnx'` (runtime alone, mirroring the backend predicate — an `object_detection` record without it is a TorchScript detector that still compiles through Neo); add `src/components/CompilationTab.detection.test.tsx`.
    - _Requirements: 7.1, 9.3_
  - [x] 6.4 `TrainingDetail.tsx`: Model Type row; headline metric switches to "Test mAP@50" / `metrics['test:mAP50']` for detection.
    - _Requirements: 7.2, 7.3_
  - [x] 6.5 `types/index.ts`: add optional `model_type`, `source`, `runtime`, `detection` to `TrainingJob`.
    - _Requirements: 7.4_

- [x] 7. Gates
  - [x] 7.1 Recompute `sha256sum edge-cv-portal/backend/functions/packaging.py` and update `sibling_spec_files` in `test/backend-test/security/baselines/iam_out_of_scope_baseline.json`; append to its note why (trained-detection packaging branch).
    - _Requirements: 9.1_
  - [x] 7.2 Run the full preservation suite (command in Notes) and all new/affected backend tests (`tests/test_detection_*`, `tests/test_onnx_*`, `tests/test_vision_model_packaging_*`, `tests/test_property_packaging_gates.py`, `tests/test_model_converter_preserve_aspect.py`); all green.
    - _Requirements: 9.4_
  - [x] 7.3 Frontend: `npx vitest run` for the new tests and `npm run build` green.
    - _Requirements: 9.3_

- [x] 8. Infrastructure
  - [x] 8.1 `compute-stack.ts`: bundle `detection_training/` into the `TrainingHandler` asset (local `fs.cpSync` bundler + amazonlinux Docker fallback), add `DETECTION_TRAINING_IMAGE` from context `detectionTrainingImage` (default `''`), set `memorySize: 512`, `timeout: 120s`, bump `CODE_VERSION`. Run `npm run build` in `edge-cv-portal/infrastructure` and confirm the IAM synth baseline test still passes (or skips when the toolchain is unavailable).
    - _Requirements: 8.1, 8.2, 8.3, 8.4, 8.5_

- [x] 9. Portal deploy — DONE 2026-09-13 ~09:32Z (account 164152369890, us-east-1)
  - Preconditions confirmed: no component build running, `cdk.out` moved to `cdk.out.bak-20260913T041952Z`, the three out-of-scope guards green (6 passed / 3 skipped).
  - Infra: `cdk deploy --all --force -c deployGroundedSamWorker=false -c cloudFrontDomain=d23v4ltibogb5x.cloudfront.net` → all 8 stacks ✅ (`INFRA_DEPLOY_OK`, rc=0). Frontend: `deploy-frontend.sh` (with the same flag on its trailing ComputeStack redeploy) → S3 synced, CloudFront invalidation `IDDZFBEY29J2WG01NF57XCXVER`, ComputeStack ✅ (`FRONTEND_DEPLOY_OK`, rc=0).
  - Verified live: `TrainingHandler` = 512 MB / 120 s, `CODE_VERSION=2026-09-13-v1`, `DETECTION_TRAINING_IMAGE=""` (regional default), bundle contains `detection_training/{train.py,requirements.txt,manifest_to_detector_dataset.py,dedupe_frames.py}`; SharedLayer v74 contains `python/detection_training.py`.
  - **Post-deploy fix (2026-09-13 14:00Z):** first real submission 500'd with botocore `Invalid length for parameter InputDataConfig, value: 0, valid min length: 1`. The key is optional on `CreateTrainingJob` but an EMPTY list is rejected client-side; the test stub had not enforced that. Fixed by omitting `InputDataConfig` entirely (`training.py`), made `FakeSageMakerService` enforce botocore's non-empty-list rule for `InputDataConfig`/`Tags`/`MetricDefinitions`, and redeployed `EdgeCVPortalComputeStack` (✅, rc=0, `TrainingHandler` UPDATE_COMPLETE 14:06Z). Everything before the SageMaker call had worked on the real request (bounding-box manifest validated, sourcedir uploaded to `s3://ryvan-cookies/models/detection-training/blue-plate-trained-imts-…/`).
    - _Requirements: 9.5_

- [ ] 10. USER ACTION — on-device verification (IN PROGRESS — paused 2026-09-13 ~14:55Z)
  - **Training half DONE and verified against real AWS.** Portal job `d62a831d-c453-4a00-b4b2-98ea12a67f0b` (SageMaker `blue-plate-trained-imts-20260913-142306`, `ml.g4dn.xlarge`, 1207 billable s) trained from the `labeling-9cbcdb4c` bounding-box manifest via the portal: 145 images pulled by `source-ref`, converter split 101/22/22, 100 epochs in 14 min. Test metrics captured by `MetricDefinitions` and hydrated onto the record by `GET /training/{id}` (the EventBridge-flipped-first ordering): **mAP@50 0.995, mAP@50-95 0.919, precision 0.999, recall 1.0** — identical to the manual reference run. Artifact `s3://ryvan-cookies/models/training/blue-plate-trained-imts-20260913-142306/blue-plate-trained-imts-20260913-142306/output/model.tar.gz` = `model.onnx` (1×3×1280×1280 → 1×5×33600) + `best.pt` + `training_metadata.json` (`preserve_aspect: true`).
  - **Remaining (resume here):** open the job in Training Detail → Component Actions → **Package Models** (expect one ZIP fanned out to jp5/jp6/jp7/x86 with `yolo_object_detection/model.onnx` and a `detection.preserve_aspect: true` manifest) → **Publish Component** → deploy to the JP7 DLAP → run the workflow → confirm detections + healthy backend for a sustained period. Record the result in `docs/detection-training-gap.md`.
  - Side findings from the run: (a) ultralytics tries to auto-install `onnxruntime-gpu` during export (harmless, wasted round-trip; pin it or set `AUTOINSTALL=false` if it bothers anyone); (b) "Created By: unknown" is pre-existing portal-wide — the `admin` Cognito user has no `email` attribute. `shared_utils.get_user_from_event` now falls back to the Cognito username (tests in `tests/test_shared_utils_user_identity.py`, 7 pass) but is **NOT deployed yet** (held to avoid colliding with the other session's ComputeStack deploys); the account-side fix is `aws cognito-idp admin-update-user-attributes --user-pool-id us-east-1_2r9jpbWIe --username admin --user-attributes Name=email,Value=<email> Name=email_verified,Value=true`.
  - Uncommitted: all portal-detection-training work + today's `InputDataConfig` fix + the `created_by` fallback share a working tree with the other session's rfdetr spec edits; commit portal-detection-training as its own commit before that session commits.
    - _Requirements: 9.5_
