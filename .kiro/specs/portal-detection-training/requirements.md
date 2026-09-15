# Requirements Document

## Introduction

The DDA portal can capture frames, label bounding boxes, package an ONNX detector as a Greengrass model component and serve it on device, but it cannot *train* a detector. The only working detector training path is `datasets/detection_training/train.py`, a SageMaker script-mode job launched by hand outside the portal; it writes no `TrainingJobs` record, so the portal's packaging and publish flow cannot see its output. `docs/detection-training-gap.md` §4–§8 enumerates the missing portal wiring.

This feature makes **Object Detection (YOLO)** a first-class portal model type: a Data Scientist selects a bounding-box manifest, starts a training job from `CreateTraining.tsx`, the backend launches the existing `train.py` entry point on a SageMaker PyTorch DLC in script mode, the finished `model.onnx` is packaged straight into a Greengrass model component (no SageMaker Neo — the artifact is already ONNX) with a device manifest that letterboxes exactly as the model was trained, and the component publishes and deploys through the flows that already exist. Every existing LFV (classification / segmentation) path stays byte-identical.

Everything here is cloud-side; no LocalServer component build is required. The one on-device change — the manifest written for the retrained model — is verified on real hardware after deploy, per the repo's edge rule.

## Glossary

- **Portal**: the edge-cv-portal web application (React frontend, Python Lambda backend, CDK infrastructure).
- **Detection_Training_Job**: a portal `TrainingJobs` record with `model_type = 'object_detection'`, created by `training.py` and backed by a SageMaker script-mode training job.
- **LFV_Training_Job**: an existing `TrainingJobs` record with one of the four marketplace model types (`classification`, `segmentation`, `classification-robust`, `segmentation-robust`), backed by the AWS Marketplace algorithm.
- **Detection_Manifest**: a JSON Lines augmented manifest whose entries carry `source-ref` plus a bounding-box attribute — either the DDA literal `bounding-box` / `bounding-box-metadata` pair written by `dda_manifest.serialize_manifest`, or a Ground Truth job-named attribute whose `-metadata` companion has `type = groundtruth/object-detection` — with `annotations` and a `class-map`.
- **Detection_Entry_Point**: `datasets/detection_training/train.py` together with `requirements.txt`, `manifest_to_detector_dataset.py` and `dedupe_frames.py`, bundled flat into `sourcedir.tar.gz` (see `build_sourcedir.sh`).
- **Detection_Training_Image**: the SageMaker PyTorch GPU Deep Learning Container that runs the Detection_Entry_Point in script mode.
- **Detection_Hyperparameters**: the user-tunable settings forwarded to the Detection_Entry_Point — `imgsz`, `epochs`, `batch`, `base_weights`, `patience` — and the device decode thresholds `score_threshold`, `iou_threshold`.
- **Detection_Record_Fields**: the fields persisted on a Detection_Training_Job so packaging needs no re-derivation: `runtime = 'onnx'` and a `detection` block holding `detection_arch`, `network_input_width`, `network_input_height`, `class_names`, `num_classes`, `score_threshold`, `iou_threshold`, `preserve_aspect`, plus the hyperparameters used.
- **Detection_Artifact**: the SageMaker `model.tar.gz` written by the Detection_Entry_Point, containing `model.onnx`, `best.pt` and `training_metadata.json`.
- **Detection_Component_Package**: the Greengrass model-component ZIP built from a Detection_Artifact: `manifest.json` at the root (runtime `onnx`, task `object_detection`, a `detection` block) and `model.onnx` nested under `yolo_object_detection/`.
- **Device_Manifest**: the `manifest.json` inside a model component that `lfv_model_template.__load_model_graph_config` reads on device; its top-level `detection` block is merged into every stage.
- **Preserve_Aspect**: the Device_Manifest flag that selects the letterbox preprocessing path in `BasicPreProcessor._preserve_aspect()` instead of the aspect-destroying squash.
- **Compilation_Bypass**: the existing behaviour in `compilation.py` / `packaging.py` / `CompilationTab.tsx` for imported BYO ONNX models: Neo is skipped, `compilation_skipped` is recorded, and the UI offers Package/Publish directly.
- **Preservation_Gate**: the `test/backend-test/security/preservation/` suite that pins sha256 hashes of security-relevant files; `packaging.py` is tracked by `iam_out_of_scope_baseline.json`.

## Requirements

### Requirement 1: Object Detection is an accepted training model type

**User Story:** As a Data Scientist, I want to submit an Object Detection training job from the portal, so that I can retrain a detector without leaving DDA.

#### Acceptance Criteria

1. WHEN `POST /training` is called with `model_type = 'object_detection'`, THE Portal SHALL accept the value and SHALL NOT return the "Invalid model_type" error.
2. WHEN `POST /training` is called with any of the four LFV model types, THE Portal SHALL behave byte-identically to the current implementation: same manifest validation, same `AlgorithmSpecification` (marketplace `AlgorithmName`), same `InputDataConfig` (`AugmentedManifestFile`, `RecordIO`, `Pipe`), same hyperparameters, same DynamoDB item.
3. IF `model_type = 'object_detection'` is submitted with `model_source` other than `'marketplace'` absent or present, THEN THE Portal SHALL treat the job as a Detection_Training_Job regardless of `model_source` (the marketplace algorithm is never used for detection).
4. WHEN a Detection_Training_Job is created, THE Portal SHALL require the `DataScientist` role on the use case exactly as it does for LFV jobs.

### Requirement 2: Detection manifests are validated as detection manifests

**User Story:** As a Data Scientist, I want the portal to accept my bounding-box manifest and tell me clearly when it is the wrong kind of manifest, so that I don't get sent to a transformer that cannot help.

#### Acceptance Criteria

1. WHEN a Detection_Training_Job is created, THE Portal SHALL validate the manifest with a detection validator and SHALL NOT call `validate_marketplace_manifest`.
2. THE detection validator SHALL accept a Detection_Manifest whose first entry has a string `source-ref`, a bounding-box attribute with a list `annotations` and an `image_size` list, and a `-metadata` companion carrying a non-empty `class-map`.
3. THE detection validator SHALL accept both the DDA literal `bounding-box` attribute name and a Ground Truth job-named attribute (identified by its `-metadata` companion's `type` being `groundtruth/object-detection`).
4. IF the first entry has `anomaly-label` but no bounding-box attribute, THEN THE Portal SHALL return HTTP 400 whose `error` states that Object Detection requires a bounding-box manifest and whose `details` name the attributes found, and SHALL NOT suggest the Manifest Transformer.
5. IF the manifest object does not exist or has no parsable first line, THEN THE Portal SHALL return HTTP 400 with an error identifying the manifest URI.
6. WHEN validation succeeds, THE detection validator SHALL return the ordered `class_names` derived from the `class-map` (sorted by integer class id) so they can be persisted on the record.
7. IF an LFV_Training_Job is submitted with a Detection_Manifest, THEN THE Portal SHALL keep the existing `validate_marketplace_manifest` behaviour unchanged (this is not a detection path).

### Requirement 3: Detection training launches the script-mode entry point

**User Story:** As a Data Scientist, I want the portal to launch the proven `train.py` job for me, so that the detector is trained letterboxed and exported to ONNX exactly as the working manual run did.

#### Acceptance Criteria

1. WHEN a Detection_Training_Job is created, THE Portal SHALL call `sagemaker.create_training_job` with `AlgorithmSpecification.TrainingImage` set to the Detection_Training_Image and SHALL NOT set `AlgorithmName`.
2. THE Detection_Training_Image SHALL be resolvable from the `DETECTION_TRAINING_IMAGE` environment variable, defaulting to the SageMaker PyTorch 2.5.1 GPU py311 CUDA 12.4 DLC in the use case's region (account `763104351884`), so the default is not pinned to `us-east-1`.
3. WHEN a Detection_Training_Job is created, THE Portal SHALL build `sourcedir.tar.gz` from the bundled Detection_Entry_Point files (all four at the archive root), upload it to `s3://{usecase.s3_bucket}/models/detection-training/{training_job_name}/sourcedir.tar.gz` with the use case's S3 client, and pass `sagemaker_program = 'train.py'` and `sagemaker_submit_directory = <that URI>` as `HyperParameters`.
4. WHEN a Detection_Training_Job is created, THE Portal SHALL pass `MANIFEST_S3` (the selected manifest URI), `IMGSZ`, `EPOCHS`, `BATCH`, `BASE_WEIGHTS`, `PATIENCE` and `ONNX_OPSET` both as `HyperParameters` and as `Environment` entries (the entry point reads bare environment names), and SHALL NOT require an `IMAGES_S3` prefix.
5. WHEN the Detection_Entry_Point runs without `IMAGES_S3`, IT SHALL download every image referenced by the manifest's `source-ref` URIs; WHEN `IMAGES_S3` is set, IT SHALL keep the existing prefix-listing behaviour.
6. WHEN a Detection_Training_Job is created, THE Portal SHALL OMIT `InputDataConfig` (the key is optional on `CreateTrainingJob`, but botocore rejects an empty list client-side — a real 500 on the first submission), set `EnableNetworkIsolation = False` (the container installs `requirements.txt` from PyPI and downloads pretrained weights), `ResourceConfig.VolumeSizeInGB = 60`, `ResourceConfig.InstanceType` from the request (default `ml.g4dn.xlarge`), `StoppingCondition.MaxRuntimeInSeconds` from the request (default `10800`), `OutputDataConfig.S3OutputPath` from `path_builder.get_training_output_uri(training_job_name)`, the same `RoleArn` and the same four `Tags` as LFV jobs.
7. WHEN a Detection_Training_Job is created, THE Portal SHALL declare `AlgorithmSpecification.MetricDefinitions` that capture `test_map50`, `test_map50_95`, `test_precision` and `test_recall` from the entry point's `TEST METRICS: {...}` log line as `test:mAP50`, `test:mAP50-95`, `test:precision`, `test:recall`.
8. IF any Detection_Hyperparameter fails validation — `imgsz` not a positive multiple of 32 in [320, 2048], `epochs` not in [1, 1000], `batch` not in [1, 64], `patience` not in [0, 1000], `base_weights` not matching `^[A-Za-z0-9._-]+\.pt$`, `score_threshold` or `iou_threshold` not in (0, 1) — THEN THE Portal SHALL return HTTP 400 naming the offending field and SHALL create no SageMaker job.
9. WHEN Detection_Hyperparameters are omitted, THE Portal SHALL use `imgsz = 1280`, `epochs = 100`, `batch = 4`, `base_weights = 'yolo11s.pt'`, `patience = 30`, `score_threshold = 0.25`, `iou_threshold = 0.45`.
10. IF `sagemaker.create_training_job` fails, THEN THE Portal SHALL map the error exactly as the LFV path does (ValidationException → 400, AccessDenied → 403, ResourceLimitExceeded → 429, otherwise 500) and SHALL write no `TrainingJobs` record.

### Requirement 4: Detection record fields are persisted for downstream steps

**User Story:** As the packaging step, I want every geometry and decode setting the device needs recorded on the training job, so that I can write a correct Device_Manifest without re-deriving anything.

#### Acceptance Criteria

1. WHEN a Detection_Training_Job is created, THE Portal SHALL write a `TrainingJobs` item with the same base fields as an LFV job (`training_id`, `usecase_id`, `model_name`, `model_version`, `model_type`, `dataset_manifest_s3`, `hyperparameters`, `instance_type`, `training_job_name`, `training_job_arn`, `status = 'InProgress'`, `progress = 10`, `created_by`, `created_at`, `updated_at`, `auto_compile`, `compilation_targets`) plus `runtime = 'onnx'`, `algorithm_uri` set to the Detection_Training_Image, and a `detection` block containing the Detection_Record_Fields with `detection_arch = 'yolo'`, `network_input_width = network_input_height = imgsz`, `preserve_aspect = True`, `class_names` from the validator (or the request's `class_names` override), and `num_classes = len(class_names)`.
2. WHEN `GET /training/{id}` is called for a Detection_Training_Job whose SageMaker status is `Completed` and whose record has no `metrics`, THE Portal SHALL copy `FinalMetricDataList` into a `metrics` map keyed by metric name and persist it, regardless of whether the status transition was already recorded by the EventBridge handler.
3. WHEN `GET /training/{id}` is called for an LFV_Training_Job, THE Portal SHALL NOT read or write `metrics` (existing behaviour preserved).
4. THE `TrainingJobs` record for a Detection_Training_Job SHALL NOT carry `source = 'imported'`; downstream code SHALL identify it by `model_type = 'object_detection'` together with `runtime = 'onnx'` and `source` absent or `'trained'`. An `object_detection` record without `runtime = 'onnx'` (a TorchScript detector, the shape earlier specs' fixtures model) SHALL keep taking the Neo / ONNX-export path unchanged.

### Requirement 5: Detection-trained models skip Neo and package directly

**User Story:** As a Data Scientist, I want a finished detection job to go straight to Package and Publish, so that I am not routed into a Neo compilation that cannot accept ONNX.

#### Acceptance Criteria

1. WHEN `POST /training/{id}/compile` is called for a completed Detection_Training_Job, THE Portal SHALL apply the Compilation_Bypass: set `compilation_skipped = True`, return HTTP 200 with `compilation_jobs = []` and `compilation_skipped = True`, and SHALL make no SageMaker call and SHALL NOT call `extract_and_repackage_model`.
2. WHEN `POST /training/{id}/package` is called for a completed Detection_Training_Job, THE Portal SHALL build one Detection_Component_Package from the Detection_Artifact and record one `packaged_components` entry per target (the requested targets, or the default `jetson-xavier-jp5`, `jetson-xavier-jp6`, `jetson-xavier-jp7`, `x86_64-cpu`), each with `status = 'packaged'` and the same `component_package_s3`.
3. THE Detection_Component_Package's `manifest.json` SHALL contain `runtime = 'onnx'`, `runtime_artifact = 'model.onnx'`, `task = 'object_detection'`, `model_graph.stages[0].type = 'yolo_object_detection'` with `input_shape = [1, 3, H, W]`, `image_width`, `image_height`, `image_range_scale = True`, `normalize = False`, `threshold = score_threshold`, `num_classes`; a `dataset` block with `image_width`/`image_height`; a `preprocessing` block with `resize = [W, H]` and `channel_order = 'RGB'`; and a `detection` block with `layout = 'yolo'`, `num_classes`, `score_threshold`, `iou_threshold`, `network_input = W`, `preserve_aspect = True`, `class_names`.
4. THE Detection_Component_Package SHALL place `model.onnx` at `yolo_object_detection/model.onnx` inside the ZIP (nested under the stage type, as `package_onnx_component` does).
5. WHEN the Detection_Artifact contains `training_metadata.json`, THE packaging step SHALL take `imgsz` from it as the authoritative network input size and log a warning if it differs from the record's `detection.network_input_width`.
6. IF the Detection_Artifact contains no `.onnx` file, THEN THE packaging step SHALL fail with HTTP 500 whose error names the missing artifact and SHALL leave `packaged_components` unchanged.
7. THE Detection_Component_Package manifest for given inputs SHALL be equal to the `export_artifacts/manifest.json` that `model_converter.generate_dda_package(export_format='onnx', model_type='object_detection', detection_arch='yolo', preserve_aspect=True, ...)` produces for the same inputs, except that the packaging manifest additionally carries the `dataset` block (parity guards against manifest drift between Smart Import and trained detection).
8. WHEN `POST /training/{id}/compile` or `/package` is called for an LFV_Training_Job or an imported model, THE Portal SHALL behave byte-identically to the current implementation.

### Requirement 6: The frontend offers Object Detection and recognizes detection manifests

**User Story:** As a Data Scientist, I want the Create Training page to offer Object Detection, accept my bounding-box manifest, and expose the few YOLO settings that matter, so that I can start the job without workarounds.

#### Acceptance Criteria

1. THE `CreateTraining` page SHALL list an "Object Detection (YOLO)" option with value `object_detection` in the Model Type select, after the four LFV options.
2. WHEN a manifest sample entry contains a bounding-box attribute (the literal `bounding-box` key, or any `*-metadata` key whose value has `type` containing `object-detection`), THE `CreateTraining` page SHALL classify the manifest format as `'detection'` and SHALL NOT classify it as `'ground-truth'`.
3. WHEN the manifest format is `'detection'`, THE `CreateTraining` page SHALL NOT show the "Ground Truth Format Detected" alert, SHALL NOT offer "Transform Manifest Now", and SHALL NOT block submission on a transform.
4. IF the selected model type is `object_detection` and the manifest format is `'dda'` or `'ground-truth'`, THEN THE `CreateTraining` page SHALL show a validation error stating that Object Detection requires a bounding-box manifest and SHALL disable Start Training.
5. IF the selected model type is an LFV type and the manifest format is `'detection'`, THEN THE `CreateTraining` page SHALL show a validation error stating that a bounding-box manifest requires the Object Detection model type and SHALL disable Start Training.
6. WHEN `object_detection` is selected, THE `CreateTraining` page SHALL default Max Runtime to `10800`, SHALL hide the "Segmentation head only" toggle and the robust-mode warning, SHALL offer detection instance types (`ml.g4dn.xlarge` default, `ml.g4dn.2xlarge`, `ml.g5.xlarge`, `ml.g5.2xlarge`) instead of the marketplace list, and SHALL show Detection_Hyperparameter inputs (image size, epochs, batch, base weights, score threshold, IoU threshold) with the Requirement 3.9 defaults.
7. WHEN `object_detection` is selected and the form is submitted, THE `CreateTraining` page SHALL send `model_type = 'object_detection'` and `hyperparameters = { imgsz, epochs, batch, base_weights, patience, score_threshold, iou_threshold }`, and SHALL NOT send `classification_logic`.
8. WHEN an LFV type is selected, THE `CreateTraining` page SHALL behave exactly as today (marketplace instance list, existing runtime defaults, seghead toggle for segmentation, Ground Truth transform gate).
9. THE `handleTransformManifest` task-type selection SHALL remain `'segmentation'` for segmentation types and `'classification'` otherwise, and SHALL never be invoked for `object_detection` (the transform button is not rendered for `'detection'` manifests).
10. THE marketplace-specific guidance text ("requires properly formatted manifests with 'anomaly-label' attributes", "Manifest Requirements", "Compile Model" next step) SHALL be replaced by detection-specific guidance when `object_detection` is selected.

### Requirement 7: Training detail and component actions understand detection jobs

**User Story:** As a Data Scientist, I want the training detail page to show my detector's mAP and let me package/publish without a compile step, so that the UI matches what the backend does.

#### Acceptance Criteria

1. WHEN `CompilationTab` renders a training job with `runtime = 'onnx'`, IT SHALL treat the job like an imported ONNX model: no "Start Compilation" empty state, Component Actions visible once `status = 'Completed'`, with the ONNX info alert. An `object_detection` job WITHOUT `runtime = 'onnx'` SHALL keep the existing Neo empty state (it mirrors Requirement 4.4's backend predicate).
2. WHEN `TrainingDetail` renders a Detection_Training_Job, IT SHALL show a Model Type row and SHALL label the headline metric "Test mAP@50" reading `metrics['test:mAP50']` instead of "Validation Accuracy".
3. WHEN `TrainingDetail` renders an LFV_Training_Job or imported model, IT SHALL render exactly as today.
4. THE `TrainingJob` TypeScript type SHALL gain optional `model_type`, `source`, `runtime` and `detection` fields so the predicates above are typed.

### Requirement 8: Infrastructure ships the entry point and the image setting

**User Story:** As an operator, I want the portal deploy to carry everything the training Lambda needs, so that no manual S3 staging is required.

#### Acceptance Criteria

1. THE `TrainingHandler` Lambda asset SHALL include a `detection_training/` directory containing `train.py`, `requirements.txt`, `manifest_to_detector_dataset.py` and `dedupe_frames.py` copied from `datasets/`, produced by CDK asset bundling with a local (no-Docker) bundler and a Docker fallback.
2. THE `TrainingHandler` environment SHALL include `DETECTION_TRAINING_IMAGE`, sourced from the CDK context key `detectionTrainingImage` when provided and otherwise an empty string (the backend then applies its regional default).
3. THE `TrainingHandler` SHALL have `memorySize = 512` and `timeout = 120` seconds to cover building and uploading the sourcedir before the SageMaker call.
4. THE change SHALL add no new IAM actions: `sagemaker:CreateTrainingJob`, `iam:PassRole` on `DDA*Role`, and S3 data-plane access already cover the detection path.
5. THE IAM CDK-synth preservation baseline for `EdgeCVPortalComputeStack` SHALL remain satisfied (memory/timeout/asset/env changes do not alter IAM statements).

### Requirement 9: Preservation gates and verification

**User Story:** As the release owner, I want the security gate to stay green and the on-device behaviour verified, so that this change ships safely.

#### Acceptance Criteria

1. WHEN `packaging.py` is modified, THE change SHALL rebaseline its sha256 in `test/backend-test/security/baselines/iam_out_of_scope_baseline.json` (`sibling_spec_files`) in the same commit, with the baseline note recording why.
2. THE backend test suite SHALL include tests for: LFV `create_training_job` request shape unchanged; detection `create_training_job` request shape and record; detection manifest validator accept/reject cases; hyperparameter validation; compile bypass for detection; package for detection (ZIP layout and manifest content); manifest parity with `generate_dda_package` (Requirement 5.7).
3. THE frontend SHALL include unit tests for the manifest-format classifier (Requirement 6.2) and the `CompilationTab` detection gating (Requirement 7.1), and `tsc && vite build` SHALL pass.
4. THE full preservation suite (excluding the `dill`-dependent roundtrip module unavailable on this host) SHALL pass after the change.
5. AFTER the portal deploy, THE retrained detector component SHALL be verified on a real Jetson device (workflow runs, detections appear, backend healthy for a sustained period) before this work is called done; this is a user action recorded in tasks.md.
