# Implementation Plan

## Notes

- This spec builds on `rfdetr-training-and-transfer-learning`, which provides the fine-tunable sidecar and `classify_checkpoint`, and on `portal-detection-training`, which provides `package_trained_detection_component`. The following must stay byte-identical, with their existing suites passing unmodified:
  - the ONNX Smart Import path;
  - the legacy `.pt` Smart Import path (`export_format='pytorch'`);
  - the trained-detection create and package paths;
  - the probe.
- `model_converter.py` and `packaging.py` are preservation-tracked (`test/backend-test/security/baselines/iam_out_of_scope_baseline.json`). Rebaseline both in the same change, with a note naming this spec.
- **Task 1 (the spike) must land before tasks 3.x, 4.1 and 5.3.** Those tasks depend on the pins and tolerances it decides. Tasks 2.x are pin-agnostic: write them with the provisional constants from design.md §1 and update the constants after 1.5.
- **Test commands.**
  - Backend, from `edge-cv-portal/backend`: `~/.venvs/dda-portal-tests/bin/python -m pytest tests/<file> -q -p no:cacheprovider`. Run targeted files only, never all of `tests/` in one process.
  - Frontend: `npx vitest run <file>`, then `npm run build`.
  - CDK: `npm test -- <file>` in `infrastructure/`.
  - The two preservation guards and the full preservation suite: see `.kiro/steering/builds.md`.
- **Conversion jobs are CPU jobs** (`ml.m5.xlarge`); no GPU quota is involved. Tasks 1, 10 and 11 need real AWS (and, for task 11, hardware), so skip them in unattended harness runs.
- **Deploy rules.**
  - Never run a portal deploy while a component build is running.
  - Move `edge-cv-portal/infrastructure/cdk.out` aside and run the guards green first.
  - Do **not** copy the older specs' `-c deployGroundedSamWorker=false`: since portal-deploy-flag-hardening, that flag tears the Grounded-SAM worker down.
  - No LocalServer build is needed for this spec.

## Task Dependency Graph

```json
{
  "waves": [
    { "wave": 1, "description": "Spike on real isolated jobs, in parallel with the pin-agnostic shared-layer pure pieces.", "tasks": ["1.1", "1.2", "1.3", "1.4", "1.5", "2.1", "2.2", "2.3", "2.4", "2.5"] },
    { "wave": 2, "description": "Export entry point, Export_Image, ECR repository.", "tasks": ["3.1", "3.2", "3.3", "4.1", "4.2"] },
    { "wave": 3, "description": "Portal backend: inspect/upload/convert, lifecycle hooks, packaging finalize.", "tasks": ["5.1", "5.2", "5.3", "5.4", "6.1", "6.2", "6.3", "7.1", "7.2", "7.3"] },
    { "wave": 4, "description": "Frontend.", "tasks": ["8.1", "8.2", "8.3", "8.4"] },
    { "wave": 5, "description": "Gates: suites, rebaselines, IAM approvals, real-job fixture run.", "tasks": ["9.1", "9.2", "9.3", "9.4"] },
    { "wave": 6, "description": "USER ACTION: build and push the Export_Image, then deploy the portal.", "tasks": ["10"] },
    { "wave": 7, "description": "USER ACTION: end-to-end through the deployed UI and on-device verification.", "tasks": ["11"] }
  ]
}
```

## Tasks

- [ ] 1. Exploration spike (Requirement 1)
  - **Re-plan after the spike (2026-09-25, `docs/detector-checkpoint-import-spike.md`):**
    - `v10Detect` is **accepted**: it exports one-to-many with `nms=None`. Task 9.4 expects v10 to pass.
    - RF-DETR converts at its size's native resolution only (Requirement 4.3).
    - The Checkpoint_Size_Cap is 512 MiB.
    - Parity runs on both onnxruntime versions, and RF-DETR is compared at detection level.
    - The base image is bookworm.
    - Task 4.1 needs **no** usecase-account ECR grant: the CDK `DDASageMakerExecutionRole` already pulls from any repository.
  - [x] 1.1 Prototype the Export_Image locally.
    - Base: `python:3.11-slim` from `public.ecr.aws`, torch/torchvision 2.5.1 CPU, a candidate ultralytics 8.4.x, rfdetr 1.10.1 with `[onnx]`, onnx, onnxslim, and a `/opt/ort-floor` venv with `onnxruntime==1.16.3`.
    - Push it to a scratch ECR repository in 164152369890 / us-east-1.
    - Stage the fixtures from 1.1(a)–(e) under a scratch prefix in `ryvan-cookies`. Verify the PPE sha256 `a00b6fce…2119` before use, and take `best.pt` and `model.onnx` from the `artifact_s3` of portal job `99f9c131-7a49-4be7-9ce3-ba98f57366a7` (blue-plate-yolo-ft-v3).
    - _Requirements: 1.1_
  - [x] 1.2 Run each fixture as a real SageMaker job with `EnableNetworkIsolation=True`, the code baked into the image and a single input channel.
    - Record everything in 1.2: load, shapes, opset/IR, fleet-floor load and run, parity, and wall-clock.
    - Confirm that `/opt/ml/output/failure` surfaces as `FailureReason`, and that nothing in the job tries the network.
    - _Requirements: 1.1, 1.2_
  - [x] 1.3 YOLO decisions.
    - The ultralytics pin, and the export arguments that give raw one-to-many output for YOLO11 and YOLO26 (expected `nms=None`; `nms=False` gives `(N, 300, 6)`).
    - The accepted and rejected head classes, `v10Detect` included.
    - The reference-model incantation for parity.
    - Trainer parity: the 8.3.40 ft-v3 `best.pt` against its own `model.onnx`.
    - _Requirements: 1.3(a)–(c), 1.3(e)_
  - [x] 1.4 RF-DETR and platform decisions.
    - The rfdetr pin; size inference from `model_name`, `args.encoder`, `args.resolution` and head shapes; the strict-load check; rejection of PML-licensed sizes; and `positional_encoding_size` at a non-native resolution.
    - The Checkpoint_Size_Cap, measured against the 29 s API timeout (download plus probe of a cap-sized file in the ModelConverter Lambda).
    - Image delivery: whether the CDK `DDASageMakerExecutionRole` needs ECR grants, and whether a cross-account repository policy works (state single-account-only if no second account exists).
    - _Requirements: 1.3(d), 1.3(f), 1.3(g)_
  - [ ] 1.5 Write `docs/detector-checkpoint-import-spike.md`: the per-fixture table, each decision with its evidence, and the final pins and tolerances. Then update the constants in design.md §1 and the *decided by the spike* criteria in requirements.md, and re-plan tasks 3.x–5.x here if the findings change them. Delete the scratch repository and prefix.
    - _Requirements: 1.4_

- [ ] 2. Shared layer: `detector_conversion.py` (pure, stdlib). Tests are in `edge-cv-portal/backend/tests/`.
  - [ ] 2.1 Implement `assess_checkpoint(probe)`: the convertibility rules in 2.3, the user-facing reasons in 2.4, the pre-fill in 2.5, and index-ordered class names in 2.6.
    - Test file: `tests/test_detector_conversion_assessment.py`.
    - Fixtures: the real PPE probe evidence (`DetectionModel`, `Detect`, `train_args {task: detect, imgsz: 640}`, `version 8.4.2`, names `[helmet, human, no-helmet, vest]`); synthetic GLOBAL sets for Segmentation, Pose, OBB, Classification, World, YOLOE, RTDETR and v10; RF-DETR R2 and R2′ evidence; TorchScript, `state_dict`, legacy, ONNX and unknown.
    - _Requirements: 2.3–2.6_
  - [ ] 2.2 Implement `validate_conversion_request` and `build_conversion_job_request`.
    - Test file: `tests/test_detector_conversion_request.py`.
    - Request validation cases: every bound in 4.3; the 4.4 rename-but-not-count rule; 4.5, where a contradicting `preserve_aspect` is a 400.
    - Job request assertions: `EnableNetworkIsolation is True`; no `sagemaker_program` or `sagemaker_submit_directory`; exactly one input channel on the sidecar prefix *with* its trailing `/`; `ml.m5.xlarge`, 30 GB and 1800 s; the environment contract from design.md §2; the tags.
    - _Requirements: 4.3–4.5, 5.1–5.4_
  - [ ] 2.3 Implement `build_conversion_record` and `is_detector_conversion_record`. Tests assert the 4.6 shape exactly, and that `is_trained_detection_record` and `is_onnx_import` are both **false** for a Conversion_Record. The ordering in 9.1 depends on this.
    - _Requirements: 4.6, 9.1_
  - [ ] 2.4 Implement `plan_conversion_transition` and `apply_conversion_transition`, following the design.md §6 table.
    - Test file: `tests/test_detector_conversion_reducer.py`.
    - Use a fake table with real conditional semantics.
    - Hypothesis over event sequences and interleaved writers asserts two properties: terminal states are never overwritten, and exactly one finalize claim is made.
    - _Requirements: 7.1–7.7_
  - [ ] 2.5 Implement `validate_conversion_artifact` with the mmap-based ONNX reader (varint helpers lifted from `checkpoint_probe.py`, leaving the probe itself unchanged).
    - Test file: `tests/test_detector_conversion_validator.py`, using synthetic tarballs and hand-assembled ONNX protobufs.
    - Cover every hostile case in 8.7, plus acceptance of YOLO `[1,8,8400]` and RF-DETR `[1,300,4]` + `[1,300,5]`, the metadata-mismatch cases in 8.4, and the sha256 agreement in 8.5.
    - _Requirements: 8.1–8.7_

- [ ] 3. Export entry point (Requirements 5, 6)
  - [ ] 3.1 Create `datasets/detection_training/export_checkpoint.py` according to design.md §2.
    - Environment contract, single-input check, sha256 gate, and `disable_autoupdate` before import.
    - YOLO: load, gate, export with the spike-pinned arguments, and the contract check.
    - RF-DETR: reuse `train_rfdetr`'s `export_onnx`, `read_onnx_io`, `verify_input` and `verify_two_outputs`; add size inference and strict load.
    - Fleet-floor subprocess, parity check, and the artifact writer (exactly `model.onnx` + `training_metadata.json` with the 6.8 keys).
    - `FATAL:` lines go to `/opt/ml/output/failure`.
    - `train.py` and `train_rfdetr.py` are not modified.
    - _Requirements: 5.3, 5.6, 5.7, 6.1–6.8_
  - [ ] 3.2 Add `tests/test_export_checkpoint_static.py`, importing the module with ultralytics, rfdetr, torch and onnxruntime stubbed (the `test_train_yolo_static.py` pattern). Assert:
    - the export kwargs: no `half`, `dynamic=False`, `simplify=True`, opset 17, and the pinned one-to-many argument;
    - that AutoUpdate is disabled before import;
    - the sha256 and single-file gates;
    - the pure contract verifiers, including embedded NMS, `(1, 300, 6)`, seg-shaped and fp16 rejections;
    - the failure-file writer;
    - the metadata keys and their agreement with what `package_trained_detection_component` reads.
    - _Requirements: 6.3–6.8, 12.2_
  - [ ] 3.3 Create `edge-cv-portal/detector-export-image/` according to design.md §3.
    - Files: a `Dockerfile` with a digest-pinned base; `requirements.lock` and `ort-floor.lock` with the task 1.5 pins; and `build-and-push.sh`.
    - The script stages a minimal context, builds `linux/amd64` with buildx, pushes to `dda-detector-export:<tag>`, and prints the `@sha256` URI.
    - Run the script against the scratch repository and repeat one fixture from 1.2 to confirm the final image.
    - _Requirements: 5.2, 5.5_

- [ ] 4. Infrastructure
  - [ ] 4.1 `compute-stack.ts`:
    - the `DetectorExportRepository` ECR repository: scan-on-push, `IMMUTABLE`, `RETAIN`;
    - a repository policy for the trusted use-case accounts' `DDASageMakerExecutionRole`, using concrete ARNs only;
    - `-c detectorExportImage` → `DETECTOR_EXPORT_IMAGE` on `ModelConverterHandler`, empty when unset;
    - `PACKAGING_FUNCTION_NAME` and `packagingHandler.grantInvoke` on both `TrainingHandler` and `TrainingEventsHandler`;
    - in `usecase-account-stack.ts`, ECR pull grants on `DDASageMakerExecutionRole`, but only if 1.4 found them missing.
    - _Requirements: 5.5, 7.5, 12.4_
  - [ ] 4.2 Add `POST /models/upload-url` → `ModelConverterHandler` (Cognito) in `api-gateway-stack.ts` and in its duplicate `api-model-stack.ts`. Add `test/detector-export-infra.test.ts` covering the repository properties, the policy principals, env-iff-context, the invoke grants, and the route in both stacks. Then run `npm run build` in `infrastructure/`.
    - _Requirements: 3.1, 12.2_

- [ ] 5. `model_converter.py` (preservation-tracked)
  - [ ] 5.1 Inspect: add the `HeadObject` cap before download. For `.pt`/`.pth`, call `classify_checkpoint` then `assess_checkpoint`, returning the `checkpoint` block and `class_names` and pre-filling the existing fields. ONNX sources are unchanged. No torch, `pickle` or `torch.load` may appear.
    - _Requirements: 2.1, 2.2, 2.5, 2.7, 2.8_
  - [ ] 5.2 Add the `get_model_upload_url` route.
    - DataScientist role check, extension and size validation, and a server-issued `model-uploads/<uuid4>/<sanitised name>` key.
    - SigV4 presign without Content-Type, TTL ≤ 15 min.
    - Move `ensure_bucket_cors` from `data_management.py` into the shared layer, and have `data_management.py` import it; its behaviour must be unchanged.
    - _Requirements: 3.1–3.7_
  - [ ] 5.3 In `convert_model`, add the conversion branch before `generate_dda_package`: re-probe → assess (400) → `validate_conversion_request` → image and region check (503) → existing sidecar code plus sha256 → SageMaker client from the assumed use-case credentials → `create_training_job` (502 with SageMaker's message on failure, no record) → `put_item(build_conversion_record(...))` → audit event → 200. The `pytorch` and ONNX branches must stay textually unchanged.
    - _Requirements: 4.1, 4.2, 4.6–4.10_
  - [ ] 5.4 Add `tests/test_model_converter_checkpoint_conversion.py` (moto, SageMaker stubbed). Cover:
    - the inspect block for the PPE fixture and for a TorchScript fixture;
    - upload-url: key shape, TTL, and the 400 and 403 cases;
    - convert: a byte-identical sidecar, exactly one job with isolation, and the record shape;
    - the 400 (seg, TorchScript), 503 (no image, other region) and 502 paths.

    `test_model_converter_fine_tunable.py` and `test_model_converter_preserve_aspect.py` must pass unmodified. Rebaseline the `model_converter.py` sha256 with a note naming this task.
    - _Requirements: 12.1–12.3_

- [ ] 6. Lifecycle hooks
  - [ ] 6.1 `training_events.handle_training_state_change`: for a record that `is_detector_conversion_record` accepts, apply the reducer through a conditional write and async-invoke Packaging with `{finalize_conversion: true}` only when the claim wins. It must never take the generic status copy or auto-compile. Other records are unchanged.
    - _Requirements: 7.3–7.6_
  - [ ] 6.2 `training.get_training_job`: add the same branch before the generic `status != job.get('status')` block, and return the post-transition record. The metrics hydration and generic paths are unchanged for non-conversion records.
    - _Requirements: 7.3–7.8_
  - [ ] 6.3 Add `tests/test_training_conversion_lifecycle.py`. It covers both writers across InProgress → Finalizing (a single invoke under a simulated race), Failed and Stopped (`FailureReason` verbatim), and terminal no-ops. It also asserts that non-conversion records keep today's updates and auto-compile, and that the existing training tests pass unmodified.
    - _Requirements: 7.1–7.8, 12.1_

- [ ] 7. Packaging and compilation
  - [ ] 7.1 In `packaging.package_components`, add the conversion block after vLLM and before `is_trained_detection_record`:
    - return 400 for InProgress or Failed;
    - otherwise: HeadObject cap → download → `validate_conversion_artifact` (on failure, Finalizing → Failed and return 422) → `package_trained_detection_component` unchanged → Finalizing → Completed (conditional), recording `packaged_components` and the `conversion.onnx_*` fields → `_trigger_component_creation` when finalizing or auto-triggered.

    The trained and ONNX-import blocks must stay textually unchanged.
    - _Requirements: 7.9, 8.1–8.6, 9.1–9.4, 9.7_
  - [ ] 7.2 In `compilation.start_compilation_job`, add `is_detector_conversion_record` to the bypass condition.
    - _Requirements: 9.1_
  - [ ] 7.3 Add `tests/test_packaging_detector_conversion.py`. Cover:
    - finalize → validate → package, where the manifest is byte-equal to a trained record's with the same Detection_Record_Fields (extending the parity approach of `test_detection_manifest_parity.py`);
    - exactly one `_trigger_component_creation`;
    - a hostile artifact → Failed, with nothing uploaded;
    - the InProgress and Failed 400s, and a Finalizing retry;
    - the compilation bypass;
    - that a Conversion_Record never matches `is_onnx_import` routing.

    Rebaseline the `packaging.py` sha256 with a note naming this task.
    - _Requirements: 9.2–9.7, 12.2, 12.3_

- [x] 8. Frontend (`edge-cv-portal/frontend/src/`)
  - [x] 8.1 Update `services/api.ts` types and add `getModelUploadUrl`. Create `utils/detectorConversion.ts` with `isDetectorConversionRecord`, `conversionStatusLabel`, `conversionLocksFor` and `validateClassNames`, and test it in `detectorConversion.test.ts`.
    - _Requirements: 10.3, 11.1_
  - [x] 8.2 `pages/SmartImport.tsx`:
    - an Upload/S3-URI toggle, with an XHR PUT and progress;
    - the Checkpoint panel;
    - for convertible checkpoints: locks and pre-fill, the class-name editor with its count locked, read-only geometry, and no compilation targets;
    - for non-convertible checkpoints: ONNX disabled with its reasons, while the base-model-only import stays available;
    - submit navigates to the record without calling packaging.

    Test file: `SmartImport.checkpoint.test.tsx` (MemoryRouter + UsecaseContext, with `apiService` mocked). It must cover index-order rendering of `helmet, human, no-helmet, vest`, the payload, and that `startPackaging` is never called.
    - _Requirements: 10.1–10.6_
    - Follow-up 2026-09-25 (user report): Smart Import and Model Import offered no ONNX export in their auto-compile pickers, although the Compilation tab did. All three now read one list, `utils/compilationTargets.ts` (tested against the compile endpoint's accepted targets). The frontend was redeployed.
  - [x] 8.3 `pages/TrainingDetail.tsx`:
    - the conversion labels and the "Conversion failed" alert;
    - the polling fix: the interval reads the latest status, not the mount-time closure, and refetches every 15 s while InProgress;
    - the Import Metadata conversion fields.

    Test file: `TrainingDetail.conversion.test.tsx`, using fake timers to prove that it refetches.
    - _Requirements: 11.1–11.3_
  - [x] 8.4 In `components/CompilationTab.tsx`, add Conversion_Records to the detection ONNX branch, and extend `CompilationTab.detection.test.tsx` accordingly. `ModelDetail.tsx` needs no change: verify that its fine-tunable badge still renders for a Conversion_Record. Finish with `npm run build`.
    - _Requirements: 11.4, 9.5, 9.6_

- [x] 9. Gates
  - [x] 9.1 Backend: all new suites green, plus these existing suites unmodified:
    - `test_checkpoint_probe.py`, `test_model_converter_fine_tunable.py`, `test_model_converter_preserve_aspect.py`, `test_model_import_fine_tunable.py`;
    - `test_detection_training_*`, `test_detection_manifest_parity.py`;
    - `test_onnx_jetson_*`, `test_onnx_compile_diagnostics_*`, `test_property_packaging_gates.py`, `test_vllm_packaging_dispatch.py`;
    - `test_train_yolo_static.py`, `test_train_rfdetr_static.py`.
    - _Requirements: 12.1, 12.2_
    - Verified 2026-09-25: 57 backend files, each in its own process (the new suites, the list above, and every suite that imports a changed module): 1,319 passed, 3 skipped (pre-existing `[HARDWARE]` deferrals in `test_property_jp6_fit_preservation.py`), 0 failed. No tracked test file is modified.
  - [x] 9.2 Move `cdk.out` aside and run the two guards green. Then run the full preservation suite at or better than baseline (`IAM_SKIP_CDK_SYNTH=1`, per the sibling specs), confirming that the `model_converter.py` and `packaging.py` rebaselines are in place and that no other pinned file changed (verify by grep; do not assume).
    - _Requirements: 12.3_
    - Verified 2026-09-25: `cdk.out` absent; guards 4 passed / 3 skipped. Preservation suite identical to a clean worktree of the base `7ea54a3`: host 140 passed / 7 skipped, flask-app container 132 passed / 8 skipped, same skip reasons. `test_preservation_model_converter.py` (skipped where torch is absent) 6 passed in the export image. Pinned sha256 of `model_converter.py` (295a015c) and `packaging.py` (2146b523) match `iam_out_of_scope_baseline.json`; no other changed file is pinned by any baseline (checked by full path, partial path and basename).
  - [x] 9.3 Run the CDK synth IAM gate. Add the new statements (the repository policy, the two invoke grants, and any ECR pull grants) to `iam_post_fix_approved_additions.json` with attribution to this spec, without regenerating the fixed baseline. Then confirm `iam_audit`, `repo_audit` and the secrets audit are green, including that no new portal code references `pickle` or `torch.load`.
    - _Requirements: 12.4_
    - Verified 2026-09-25: live `cdk synth` IAM gate 11 passed. The only new statements are the two `lambda:InvokeFunction` grants on `PackagingHandler` (TrainingHandler, TrainingEventsHandler), now approved in `iam_post_fix_approved_additions.json`. The ECR repository policy is a resource policy outside the IAM statement multiset, and no ECR pull grant was needed. The build-custom.sh security gate replayed in flask-app: `repo_audit`, `secrets_audit`, `iam_audit`, `s3_squat_audit`, `docker_base_image_audit`, `dependency_audit` and their exploration / negative-fixture suites green, identical to base. Its auth / user-management unit tests fail identically on base (46 failed, `libtritonserver.so` absent off-device). New portal code: no `pickle` import, no `torch.load`, no torch / ultralytics / rfdetr import.
  - [x] 9.4 Real-job fixture run with the final image: submit each Requirement 1.1 fixture through `build_conversion_job_request` (the portal's own request builder) and assert the spike's expected verdicts. PPE and blue-plate pass; YOLO26 passes one-to-many; the seg checkpoint fails with its `FATAL:` reason; v10 and RF-DETR behave as the spike decided. Record the results in the spike doc.
    - _Requirements: 1.2, 6.1–6.7_
    - Verified 2026-09-25 on `dda-detector-export-spike@sha256:babcfbe0…2498` (build `dci-final1`): all ten fixtures as expected through the portal's own pre-flight, request builder and artifact validator, including YOLOv10 passing one-to-many and the seg checkpoint failing with `FATAL: checkpoint task is 'segment'…`. Results in the spike doc, "Final-image gate (task 9.4)".

- [x] 10. USER ACTION: build the image and deploy the portal
  - Confirm that no component build is running (`pgrep -af "gdk component build"`, `pgrep -af build-custom.sh`).
  - Deploy the CDK repository first, or create it with an infrastructure deploy without `detectorExportImage`. Then run `edge-cv-portal/detector-export-image/build-and-push.sh` and capture the `@sha256` URI.
  - Move `cdk.out` aside and run the guards green, then deploy infrastructure and frontend with `-c detectorExportImage=<uri>`.
  - Verify:
    - `ModelConverterHandler` has `DETECTOR_EXPORT_IMAGE` set;
    - `TrainingHandler` and `TrainingEventsHandler` have `PACKAGING_FUNCTION_NAME` set;
    - `POST /models/upload-url` answers;
    - the frontend bundle carries the Checkpoint panel.
  - _Requirements: 12.5_
  - Done 2026-09-25, from `integration/all-specs` 617950a (the live portal, which another session deployed from it) plus this spec's uncommitted work. The work was fast-forwarded onto 617950a first. `train.py` and `iam_out_of_scope_baseline.json` conflicted and were resolved: the trainer runs the fleet-floor IR lowering, then upstream's IR-10 ceiling; `packaging.py` was rebaselined bb601d4d → 78d8f603. All gates were re-run on the rebased tree: backend 57 files green, preservation 142 passed / 5 skipped with live synth, IAM synth gate 11 passed, security audits green, jest 24 / 259, vitest 211 / 2,169, build.
  - `deploy-infrastructure.sh` updated all nine stacks. `build-and-push.sh --push` produced `dda-detector-export@sha256:2b6dba2a85f5907ef1f1778d008a757ca9c9818246a8f91d7cee7e4ab01ae207` (build `dci-final2`; code byte-identical to the tree), and the 9.4 fixture gate re-ran on that exact digest: all ten as expected. The digest is stored in SSM `/dda-portal/detector-export-image`, which `bin/app.ts` now reads as the default for `-c detectorExportImage`, so flag-less deploys keep it. `DETECTOR_EXPORT_IMAGE=<digest> deploy-frontend.sh` then published the frontend and redeployed the compute stack.
  - Verified: `ModelConverterHandler` DETECTOR_EXPORT_IMAGE = the digest; `TrainingHandler` and `TrainingEventsHandler` PACKAGING_FUNCTION_NAME = PackagingHandler; `POST /models/upload-url` answers 401 unauthenticated and 204 to CORS preflight (an unknown route answers 403); the live bundle carries the Checkpoint panel strings. The repository policy grants pull only, to `DDASageMakerExecutionRole`. Inspect on the live Lambda for the PPE `best.pt` (sha256 a00b6fce…2119) returns `ultralytics_checkpoint`, detect, ultralytics 8.4.2, `helmet, human, no-helmet, vest`, 640, convertible.

- [ ] 11. USER ACTION: end-to-end through the deployed UI and on-device verification
  - (a) Upload the PPE `best.pt` in Smart Import. The Checkpoint panel must show `helmet, human, no-helmet, vest` in that order, at 640, from ultralytics 8.4.2. Then convert, and watch Converting → Validating and packaging → Completed, with `model-ppe-detection` published for jp5, jp6, jp7 and x86_64-cpu.
  - (b) Deploy to `jetson-thor1`. Pin the checkpoint's `sample_image.jpg` to the Static_Image_Camera, and run a workflow using the model at least 10 times. The labels must come only from the 4 classes. The boxes and confidences must match ultralytics `predict` on the same image, run off-device with the source checkpoint, within the recorded tolerance. The backend must stay healthy with no restart.
  - (c) Verify the same component on a JP5 or JP6 device if one is online. Otherwise, state that JP5 and JP6 are covered only by the in-job fleet-floor check.
  - (d) Re-import the `best.pt` from `blue-plate-yolo-ft-v3-20260922-172408` through this path and deploy it. On `jetson-thor1`'s pinned blue-plate frame, its detections must equal those of the existing `model-blue-plate-yolo-ft-v3-jetson-xavier-jp7` component: 0.769 / 0.932 / 0.915, 3/3 plates.
  - (e) Restore the previously pinned static image. Record the results in `.kiro/specs/detector-checkpoint-import/verification-notes.md`, and tick tasks 10 and 11.
  - _Requirements: 13.1–13.5_
  - Portal side of (a), 2026-09-26, on the deployed portal: the deployed Lambdas were driven with the requests the Smart Import page sends (upload-url, presigned PUT, inspect, convert). The PPE `best.pt` (sha256 a00b6fce…2119) inspected as convertible (ultralytics 8.4.2, `helmet, human, no-helmet, vest`, 640). Its Conversion_Record reached Completed about 2 minutes after convert through the SageMaker state-change path, packaged for jp5, jp6, jp7 and x86_64-cpu, and published `model-dci-e2e-ppe-detection-*` 1.0.0 (IR 8, opset 17, `[1, 8, 8400]`, exporter ultralytics 8.4.162). The owner then re-imported the PPE model through the UI after a hard refresh, and it converted. An earlier UI attempt had run on a tab still holding the pre-deploy bundle.
  - Still open: (b)–(e) on device (thor1; JP5 / JP6), then `verification-notes.md`.
