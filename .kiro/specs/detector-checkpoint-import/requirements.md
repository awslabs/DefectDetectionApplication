# Requirements Document

## Introduction

Smart Import accepts a `.pt` / `.pth` detector checkpoint today but cannot make it deployable. `rfdetr-training-and-transfer-learning` (Req 7) taught it to recognise ultralytics and RF-DETR checkpoints and keep them as **base models** for training; nothing converts them to the ONNX graph the device serves. Worse, choosing ONNX output for a `.pt` byte-copies the checkpoint into the package under the name `model.onnx` (`model_converter.generate_dda_package`), and the device then fails to load it.

The pressing case is third-party detectors. `melihuzunoglu/ppe-detection` on Hugging Face ships only `best.pt`: a YOLO11n trained at 640 px with 4 classes, saved by ultralytics **8.4.2**. The portal's own trainers pin 8.3.40. Running `classify_checkpoint` on that file also showed that the README lists the classes in the wrong order: the index order is `helmet, human, no-helmet, vest`. A hand-typed class list would have labelled every helmet "human" on the device, and nothing would have reported an error.

This spec adds **checkpoint conversion**: Smart Import takes a detector checkpoint, converts it to ONNX in the cloud, validates the result against what the device decoders accept, and publishes it as a model component through the packaging path already verified on hardware for portal-trained YOLO and RF-DETR (`packaging.package_trained_detection_component`).

Conversion loads an **untrusted pickle**. Ultralytics checkpoints cannot be loaded with `weights_only`, so loading one executes code chosen by whoever produced the file. The central design constraint is therefore where that load happens. It happens only inside a SageMaker job with network isolation: no network, no AWS credentials. Everything the job produces is treated as attacker-controlled until the portal has validated it.

No LocalServer component build is required. The change is portal-side (Lambdas, CDK, frontend) plus a new container image, and each converted model reaches the device as its own Greengrass model component.

### Out of scope

- **Import by Hugging Face model ID.** Fetching from the hub needs outbound network from a Lambda or from the job, and the job is deliberately network-isolated. It would also need tokens for gated repos, file selection within a repo, and a supply-chain trust decision. Requirement 3 (browser upload) covers the Hugging Face case with one manual download. This is a follow-up spec candidate.
- **Non-detection tasks.** Segmentation, pose, oriented boxes and classification checkpoints are out, as are ultralytics open-vocabulary (World/YOLOE) and ultralytics RT-DETR models. On the device they decode silently wrong (see Requirement 2.4).
- **End-to-end one-to-one YOLO outputs `(N, 300, 6)`**, such as YOLO26 with `nms=False` or YOLOv10's default. These would need a new device decoder and therefore a LocalServer build.
- **Rectangular network inputs.** The detection packager and decoder are square-only on this path.
- **TensorRT execution.** It stays opt-in via `device: "tensorrt"`, exactly as today. The converter never writes it.
- **Stopping or retrying a conversion in place.** There is no `sagemaker:StopTrainingJob` grant. To retry, re-import.
- **Evaluating model licences.** The portal records provenance but does not judge licences (the PPE weights are AGPL-3.0).
- **Recommended follow-ups, not done here.** Making `train.py`'s YOLO output check fatal. It passes `nms=False`, which on ultralytics ≥ 8.4 selects YOLO26's one-to-one head, and its warn-only check would not catch that. Running base-model *training* from imported checkpoints under network isolation too. The trainers unpickle imported checkpoints today with network egress and `DDASageMakerExecutionRole` credentials.

## Glossary

- **Detector_Checkpoint**: a `.pt` / `.pth` file that `classify_checkpoint` classifies as `ultralytics_checkpoint` or `rfdetr_checkpoint`.
- **Checkpoint_Probe**: `classify_checkpoint` (`edge-cv-portal/backend/layers/shared/python/checkpoint_probe.py`). It walks the pickle literally, using stdlib only. It never executes code and never raises.
- **Convertible_Checkpoint**: a Detector_Checkpoint that passes pre-flight (Requirement 2.3).
- **Conversion_Job**: the SageMaker training job that loads a Convertible_Checkpoint and exports ONNX.
- **Export_Image**: the pinned container image the Conversion_Job runs.
- **Conversion_Record**: the `TrainingJobs` record of one conversion. It has `source = 'imported'`, `model_type = 'object_detection'`, `runtime = 'onnx'` and a `conversion` block.
- **Conversion_Status**: `conversion.status`. One of `InProgress` (job running), `Finalizing` (portal validating and packaging), `Completed` or `Failed`.
- **Conversion_Artifact**: the Conversion_Job's `model.tar.gz`, containing exactly `model.onnx` and `training_metadata.json`.
- **Device_Output_Contract**: what the device decoders accept.
  - **YOLO** (`YoloDetectionPostProcessor`): the decoder reads only `output[0]`, which must be `[1, 4+C, N]` or `[1, N, 4+C]` with `4 + C < N`. Boxes are cxcywh in network pixels. Class scores are already sigmoided. There is no objectness column and no embedded NMS.
  - **RF-DETR** (`RfDetrDetectionPostProcessor`): two outputs, `[1, Q, 4]` normalised cxcywh boxes and `[1, Q, C + 1]` logits.
- **Fleet_Floor_Runtime**: the oldest ONNX Runtime any default packaging target runs. That is onnxruntime **1.16.3**: the JP5 GPU build (`Dockerfile.jp5`) and the CPU / x86 images. It supports ONNX opset ≤ **19** and IR version ≤ **9** (onnxruntime.ai compatibility table). JP6 runs 1.20.1 and JP7 1.23.2.
- **Parity_Check**: comparing the exported graph's outputs with the source model's outputs on the same inputs.
- **Network_Input (S)**: the square input edge the graph is exported at.
- **Checkpoint_Size_Cap**: the largest source checkpoint accepted: **512 MiB**, decided by the spike (Requirement 1.3(f)). A 1 GiB checkpoint measured 27 s of download, hash, probe and sidecar upload in a ModelConverter-shaped Lambda, which leaves no margin under the 29 s API timeout (`docs/detector-checkpoint-import-spike.md`).

## Requirements

### Requirement 1: Exploration spike — evidence before the pins

**User Story:** As the team, we want measured evidence of how each checkpoint family loads and exports inside a network-isolated job before we pin versions, so that the converter does not produce graphs the device silently mis-decodes.

#### Acceptance Criteria

1. THE spike SHALL build a prototype Export_Image and run real SageMaker jobs with `EnableNetworkIsolation=True` over this fixture set:
   - (a) the PPE `best.pt` (sha256 `a00b6fce124e63c5d23f44792593983b70e646008b58bc54cf5b0a1c87ba2119`, 5,475,290 bytes, ultralytics 8.4.2);
   - (b) the `best.pt` (ultralytics 8.3.40, imgsz 1280) from portal job `99f9c131-7a49-4be7-9ce3-ba98f57366a7` (`blue-plate-yolo-ft-v3-20260922-172408`), with that job's own `model.onnx` as the **trainer-parity oracle**. Its component is already verified on `jetson-thor1` (`docs/detection-training-gap.md`);
   - (c) the published `yolov8n.pt`, `yolo11n.pt`, `yolo26n.pt` and `yolov10n.pt`;
   - (d) a published ultralytics segmentation checkpoint (`yolo11n-seg.pt`);
   - (e) the published RF-DETR nano `.pth` (R2) and the portal-trained `checkpoint_best_total.pth` (R2′).
2. For each fixture THE spike SHALL record:
   - whether it loads under the candidate ultralytics / rfdetr pins;
   - the exported input and output shapes;
   - the ONNX opset and IR version;
   - whether the graph loads and runs on the Fleet_Floor_Runtime;
   - the Parity_Check differences;
   - export wall-clock time, and job wall-clock time including provisioning.
3. THE spike SHALL decide and record:
   - (a) the exact ultralytics version the Export_Image pins, and the export arguments that produce the raw one-to-many output on it. On ultralytics ≥ 8.4, `nms=False` selects YOLO26's one-to-one head `(N, 300, 6)` and `nms=None` exports raw one-to-many (per the ultralytics end-to-end detection guide);
   - (b) the accepted ultralytics head classes: `Detect` at minimum, and `v10Detect` only if a one-to-many export exists for it;
   - (c) whether, under the chosen pin, the 8.3.40 checkpoint from (b) exports to a graph numerically equivalent to its trainer's `model.onnx`;
   - (d) the rfdetr version, and how the RF-DETR size is inferred from a checkpoint (`model_name`, `args.encoder`, `args.resolution`, head shapes), including how PML-licensed sizes are recognised and rejected;
   - (e) the Parity_Check tolerances;
   - (f) the Checkpoint_Size_Cap that keeps inspect and convert inside the API Gateway integration timeout;
   - (g) how the Export_Image reaches a use-case account (repository policy and role grants), and whether that was verified cross-account or single-account only.
4. THE findings SHALL be written to `docs/detector-checkpoint-import-spike.md`. Before implementation of the dependent tasks starts, the criteria below marked *decided by the spike* and the task list SHALL be updated to match.

### Requirement 2: Checkpoint pre-flight on inspect

**User Story:** As a Data Scientist, I want Smart Import to tell me what my `.pt` actually is (family, task, classes, input size) and whether it can be converted, before anything runs.

#### Acceptance Criteria

1. WHEN `POST /api/v1/models/inspect` receives a source whose key ends in `.pt` or `.pth`, THE Portal SHALL classify the downloaded file with the Checkpoint_Probe and return `inspection_result.checkpoint` with these fields: `kind`, `arch`, `task`, `model_class`, `head_classes`, `num_classes`, `class_names`, `train_input_size`, `framework`, `framework_version`, `convertible`, `reasons`.
2. On this path THE Portal SHALL NOT import `torch`, `ultralytics` or `rfdetr` in any portal Lambda, and SHALL NOT deserialize the file by any means that can execute code. Classification is by the Checkpoint_Probe alone.
3. `convertible` SHALL be true if and only if one of these holds:
   - (a) `kind = 'ultralytics_checkpoint'`, and:
     - the model class is `ultralytics.nn.tasks.DetectionModel`;
     - every detection-head class among the pickle GLOBALs is in the accepted set and none is a rejected head. The accepted set, decided by the spike (Requirement 1.3(b)), is `ultralytics.nn.modules.head.Detect` (which YOLO26 also uses) and `ultralytics.nn.modules.head.v10Detect`, converted through its trained one-to-many branch;
     - `train_args.task` is `detect` or absent;
     - `num_classes` and `class_names` were recovered with `len(class_names) == num_classes`.
   - (b) `kind = 'rfdetr_checkpoint'`, and:
     - `num_classes` was recovered from `class_embed.bias`;
     - no segmentation-head keys are present;
     - the inferred size is not PML-licensed (Requirement 1.3(d)).
4. WHEN `convertible` is false, `reasons` SHALL name every failed condition in user-facing language:
   - TorchScript ("a frozen graph — export ONNX with the tool that produced it, or import that ONNX");
   - plain `state_dict` / legacy torch ("weights without a model definition");
   - ONNX mislabelled as `.pt`;
   - each ultralytics non-detect model class, named by task: `SegmentationModel` segmentation, `PoseModel` pose, `OBBModel` oriented boxes, `ClassificationModel` classification, `WorldModel` / `YOLOEModel` open-vocabulary, `RTDETRDetectionModel` ultralytics RT-DETR;
   - unrecognised files.
5. For a Convertible_Checkpoint THE response SHALL pre-fill the existing fields `suggested_type = 'object_detection'`, `detection_arch`, `num_classes`, `class_names`, and `input_width = input_height = train_input_size`. `train_input_size` is `train_args.imgsz` for YOLO and `args.resolution` for RF-DETR, falling back to the native resolution of the size named by `model_name`. It SHALL be null unless it is a multiple of 32 within the arch's bounds (YOLO [320, 2048], RF-DETR [224, 1120]). An RF-DETR checkpoint whose `args.resolution` is not its size's native resolution is not convertible (Requirement 4.3).
6. `class_names` SHALL be in class-index order: ultralytics `names` keys ascending, RF-DETR `args.class_names` as stored.
7. IF the source object is larger than the Checkpoint_Size_Cap, THEN THE Portal SHALL reject it with HTTP 400 naming the size and the cap, based on `HeadObject` and before any download.
8. ONNX sources SHALL keep today's `inspect_onnx_model` path byte-for-byte.

### Requirement 3: Upload a checkpoint from the browser

**User Story:** As a Data Scientist, I want to upload a `.pt` from my machine in Smart Import, so that I don't have to copy it into the use-case bucket with S3 tooling first.

#### Acceptance Criteria

1. `POST /api/v1/models/upload-url` with `{usecase_id, file_name, size_bytes}` SHALL require the DataScientist role on the use case (the same check as convert) and return `{upload_url, model_s3_uri, expires_in}` for a presigned PUT.
2. THE object key SHALL be issued by the server: `model-uploads/<uuid4>/<sanitised file_name>` in the use case's `s3_bucket`. The client never chooses the bucket or prefix.
3. IF `file_name` does not end in `.pt`, `.pth` or `.onnx` (case-insensitive), or `size_bytes` exceeds the Checkpoint_Size_Cap, THEN THE Portal SHALL return HTTP 400 naming the problem and issue no URL.
4. THE URL SHALL expire within 15 minutes. It SHALL be signed with SigV4 without signing Content-Type, following the static-image pin precedent (`camera_registry.PIN_S3_CLIENT_CONFIG`; SigV2 presigns in us-east-1 sign Content-Type and fail browser PUTs).
5. IF the bucket's CORS configuration does not allow a PUT from the portal origin, THEN THE Portal SHALL configure it exactly as `data_management.get_upload_url` does (`ensure_bucket_cors`) before returning the URL.
6. A presigned PUT cannot bound the uploaded size, so inspect and convert SHALL enforce the Checkpoint_Size_Cap on every source with `HeadObject` (Requirement 2.7), uploads included.
7. Uploaded keys SHALL pass `is_trusted_model_source` unchanged, because they live in the use case's own bucket.

### Requirement 4: Starting a conversion

**User Story:** As a Data Scientist, I want "Convert & import" on a detector checkpoint to start an ONNX conversion and give me a record I can follow, so that I end up with a deployable model instead of a package the device cannot load.

#### Acceptance Criteria

1. WHEN `POST /api/v1/models/convert` receives a `.pt` / `.pth` source with `export_format = 'onnx'`, THE Portal SHALL re-classify the source server-side and SHALL NOT trust the inspect result the client saw. IF the source is not convertible, THEN THE Portal SHALL return HTTP 400 with the Requirement 2.4 reasons and create no record, sidecar or job. Today this request byte-copies the checkpoint into a package as `model.onnx`.
2. WHEN the source is convertible, THE Portal SHALL, in this order:
   - (a) write the fine-tunable sidecar exactly as today: `converted-models/<safe_model_name>-<hex8>/checkpoint.<ext>`, identical bytes, same `metadata.fine_tunable` shape;
   - (b) compute the sidecar's sha256;
   - (c) start the Conversion_Job (Requirement 5) with the sidecar's prefix as its only input;
   - (d) create the Conversion_Record;
   - (e) return HTTP 200 with `{training_id, model_name, status: 'InProgress', conversion: {status: 'InProgress', job_name}, fine_tunable}` without waiting for the job.
3. THE request SHALL be validated at the boundary. Any violation SHALL return HTTP 400 and create nothing. The rules:
   - `model_type` is `object_detection`;
   - S is a multiple of 32 in [320, 2048] for YOLO. For RF-DETR, S is the native resolution of the checkpoint's size (nano 384, small 512, medium 576, large 704), or one of those four when pre-flight could not infer the size. This was *decided by the spike*: rfdetr 1.10.1 re-derives `positional_encoding_size` from an explicit `resolution`, so a non-native export no longer loads the checkpoint's positional grid;
   - `score_threshold` is in (0, 1);
   - `iou_threshold` is in (0, 1) for YOLO and absent for RF-DETR;
   - `class_names`, when present, is exactly `num_classes` non-empty strings, and it is required when the checkpoint carries no names.
4. `class_names` SHALL default to the checkpoint's names in index order. A supplied list MAY rename classes but SHALL NOT change their count.
5. Geometry SHALL be derived from the arch, not taken from the request. YOLO gets `preserve_aspect = true` (ultralytics letterbox). RF-DETR gets `preserve_aspect = false` with ImageNet normalisation. IF a request `preserve_aspect` contradicts the arch, THEN THE Portal SHALL reject it with HTTP 400.
6. THE Conversion_Record SHALL carry:
   - `source = 'imported'`, `model_type = 'object_detection'`, `runtime = 'onnx'`, `status = 'InProgress'`, `progress = 10`;
   - `training_job_name` and `training_job_arn` set to the Conversion_Job;
   - the Detection_Record_Fields a portal-trained record carries: `detection.{detection_arch, network_input_width, network_input_height, class_names, num_classes, score_threshold, iou_threshold | top_k, preserve_aspect, onnx_opset}`, plus `rfdetr_size` and `resolution` for RF-DETR;
   - `metadata.{framework: 'PYTORCH', framework_version, model_file, fine_tunable}`;
   - `conversion.{status, job_name, export_image, source_s3, source_sha256, source_bytes, started_at}`.
7. IF the Conversion_Job cannot be created (quota, permissions, missing image), THEN THE Portal SHALL return an error that includes SageMaker's reason and SHALL leave no `InProgress` record.
8. IF no Export_Image is configured for the deployment, THEN convert SHALL return HTTP 503 `Checkpoint conversion is not configured on this portal (no detector export image)`. Inspect SHALL keep working and report `convertible: false` with that reason.
9. These SHALL behave byte-identically to today, with their existing tests passing unmodified:
   - the `export_format = 'pytorch'` path for `.pt` / `.pth` (base-model-only import with the sidecar);
   - the ONNX-source path;
   - `POST /models/import`.
10. THE converter SHALL write the Conversion_Record itself. The synchronous ModelImport invoke with forwarded claims is not used on this path.

### Requirement 5: The conversion job sandbox

**User Story:** As the portal operator, I want untrusted checkpoints deserialized only where a malicious one can do nothing but produce a bad file, so that importing a model can never compromise the portal, the use-case account, or other users' data.

#### Acceptance Criteria

1. THE Conversion_Job SHALL be a SageMaker training job in the use case's account, running under the existing `DDASageMakerExecutionRole` and created with `EnableNetworkIsolation=True`. The container therefore has no network and no AWS credentials.
2. THE Conversion_Job SHALL run the Export_Image with the conversion code and every dependency pre-installed. It SHALL NOT use `sagemaker_submit_directory` or a `requirements.txt`, and SHALL NOT install or download anything at run time.
3. THE Conversion_Job's only input channel SHALL be the sidecar prefix (with a trailing `/`), which holds exactly the one checkpoint object. THE job SHALL exit FATAL unless exactly one file is present and its sha256 equals the recorded `conversion.source_sha256`.
4. THE Conversion_Job SHALL be bounded: one CPU instance (initially `ml.m5.xlarge`), `VolumeSizeInGB` ≤ 30, and `MaxRuntimeInSeconds` ≤ 1800 (the `_start_onnx_export_job` precedent).
5. THE Export_Image SHALL:
   - pin every Python dependency to an exact version (the versions *decided by the spike*: torch 2.5.1+cpu, ultralytics 8.4.162, rfdetr[onnx] 1.10.1, onnx 1.23.0, onnxslim 0.1.96, onnxruntime 1.30.0, plus onnxruntime 1.16.3 in the floor venv; the full lists are `requirements.lock` and `ort-floor.lock`);
   - be built from an ECR-hosted base image pinned by digest;
   - live in a portal-managed ECR repository with scan-on-push and immutable tags.

   The image digest SHALL be recorded on every Conversion_Record.
6. THE job SHALL write a one-line, user-facing reason prefixed `FATAL:` to `/opt/ml/output/failure` for every failure it detects. SageMaker surfaces this as `FailureReason`.
7. THE job SHALL disable ultralytics AutoUpdate and auto-install (the trainers' `disable_autoupdate`) before importing ultralytics, so that an isolated job never attempts a network install.

### Requirement 6: Export and in-job verification

**User Story:** As a Data Scientist, I want the converted ONNX to be exactly what the device decoder expects and numerically faithful to my checkpoint, so that the model behaves on the device the way it did where it was trained.

#### Acceptance Criteria

1. After loading, THE job SHALL confirm authoritatively that the checkpoint is a detector, and SHALL exit FATAL naming what it found otherwise:
   - ultralytics: `model.task == 'detect'` and the head is an accepted class;
   - RF-DETR: a detection model of an accepted size whose checkpoint state_dict loads with no missing keys, no unexpected keys and no shape mismatches.
2. THE job SHALL confirm that the loaded model's class count equals the record's `num_classes`, and SHALL exit FATAL quoting both otherwise.
3. THE YOLO export SHALL use:
   - square S, taken from the record;
   - static batch 1, `dynamic=False`;
   - float32 graph inputs and outputs (never `half`; on device, float16 overflows the NMS area arithmetic for boxes above ~256×256 px);
   - `simplify=True`, opset 17;
   - the arguments *decided by the spike* (Requirement 1.3(a)) that select the raw one-to-many output with no embedded NMS: `nms=None` on ultralytics 8.4.162. `nms=False` selects the one-to-one head of YOLO26 and YOLOv10 and yields `[1, 300, 6]`.
4. THE RF-DETR export SHALL use the trainer's `export_onnx`: static `[1, 3, R, R]`, `batch_size=1`, `dynamic_batch=False`, opset 17.
5. THE job SHALL verify the Device_Output_Contract on the produced graph, and SHALL exit FATAL quoting the shapes if it fails:
   - YOLO: exactly one input `[1, 3, S, S]` of type float32, and exactly one output `[1, 4+C, N]` or `[1, N, 4+C]` with `C = num_classes` and `4 + C < N`;
   - RF-DETR: the trainer's `verify_input` and `verify_two_outputs`, which require `[1, Q, 4]` + `[1, Q, C + 1]`.
6. THE job SHALL load and run the graph once on the Fleet_Floor_Runtime (onnxruntime 1.16.3, CPU). It SHALL exit FATAL if loading fails, if any output contains NaN or Inf, if `ir_version` > 9, or if the default-domain opset > 19.
7. THE job SHALL perform a Parity_Check. It SHALL run the source model, in the same export mode the exporter traces, and the exported graph on the same deterministic inputs: a seeded synthetic tensor and a real image bundled in the Export_Image. It SHALL exit FATAL if any output differs beyond the tolerances *decided by the spike*. The measured maximum differences SHALL be written to the metadata. The spike decided these rules; the graph runs on both the exporter's own onnxruntime and the Fleet_Floor_Runtime, and each run is compared with the source model:
   - YOLO: element-wise over the whole `[1, 4+C, N]` output, box within 0.1 px and score within 1e-3;
   - RF-DETR: detection-level, because the encoder's top-k query selection can reorder near-tied, low-confidence proposals between runtimes. Every query/class pair with score ≥ 0.25 SHALL match one-to-one (same class, nearest box) within box 1e-3 (normalised) and score 5e-3, with no extra pairs, and at least 75 % of the query slots SHALL have a one-to-one partner within the same tolerances.
8. `/opt/ml/model` SHALL contain exactly `model.onnx` and `training_metadata.json`.
   - The metadata SHALL carry the keys `package_trained_detection_component` reads, with the trainers' meaning: `detection_arch`; `imgsz` (YOLO) or `resolution` (RF-DETR); `onnx_output_shape` / `onnx_output_shapes`; `num_classes`; `class_names`; `device_manifest_hints`; and `top_k` (RF-DETR).
   - It SHALL also carry `source_sha256`, `onnx_sha256`, `exporter` (library and version), `opset`, `ir_version` and the Parity_Check results.
   - The source checkpoint SHALL NOT be copied into the artifact; the sidecar is the fine-tunable copy.

### Requirement 7: Conversion lifecycle

**User Story:** As a Data Scientist, I want the imported model's status to say truthfully whether it is converting, being checked, ready or failed, and why, so that I don't have to be on the right page at the right moment to find out.

#### Acceptance Criteria

1. Conversion_Status SHALL move only along `InProgress → Finalizing → Completed`, `InProgress → Failed` or `Finalizing → Failed`. `Completed` and `Failed` are terminal and SHALL never be overwritten by a later event or sync.
2. THE top-level `status` SHALL be:
   - `InProgress` while Conversion_Status is `InProgress` or `Finalizing`;
   - `Completed` only after validation (Requirement 8) and packaging (Requirement 9) have both succeeded;
   - `Failed` otherwise.

   The Models list shows `Completed` records only, so it therefore never shows an unvalidated conversion.
3. BOTH existing status writers, `training_events.handle_training_state_change` (EventBridge) and `training.get_training_job` (sync-on-read), SHALL route a record that has a `conversion` block through one shared reducer instead of copying SageMaker's status. Records without a `conversion` block SHALL behave exactly as today.
4. WHEN SageMaker reports the Conversion_Job `Failed` or `Stopped`, THE reducer SHALL set Conversion_Status and `status` to `Failed`, and set `failure_reason` to SageMaker's `FailureReason` verbatim (which carries the job's `FATAL:` line).
5. WHEN SageMaker reports `Completed`, THE reducer SHALL claim the `InProgress → Finalizing` transition with a DynamoDB conditional write and record `artifact_s3`. Only the caller whose claim succeeded SHALL invoke the Packaging Lambda asynchronously to finalize. Finalization therefore happens exactly once, even when EventBridge delivery and sync-on-read race.
6. EVERY Conversion_Status write SHALL be conditional on the expected prior value. A failed condition is a no-op, not an error.
7. `progress` SHALL be 10 in `InProgress`, 80 in `Finalizing`, 100 in `Completed` and 0 in `Failed`.
8. THE Conversion_Job's CloudWatch logs SHALL be viewable in the record's Logs tab, because `training_job_name` is the Conversion_Job.
9. IF a record stays in `Finalizing` because the finalize invoke was lost, THEN the existing Package action (`POST /training/{id}/package`) SHALL finalize it.

### Requirement 8: Validating the untrusted artifact

**User Story:** As the portal operator, I want everything the conversion job produced treated as attacker-controlled until checked, so that a malicious checkpoint cannot smuggle anything into a published component.

#### Acceptance Criteria

1. THE Packaging Lambda SHALL run `HeadObject` on the Conversion_Artifact before downloading it, and SHALL reject an artifact above a size cap (initially 2 GiB).
2. THE tarball SHALL contain exactly the regular-file members `model.onnx` and `training_metadata.json`, after `./` normalisation.
   - Any symlink, hard link, device, absolute path, `..` component or extra member SHALL fail the conversion.
   - THE Portal SHALL read only those two members' bytes. It SHALL NOT use `extractall`. A symlink to `/proc/self/environ` must never be able to copy Lambda credentials into a component.
3. `model.onnx` SHALL be validated against the record with a torch-free ONNX reader. Any mismatch SHALL fail the conversion. The checks:
   - it is a protobuf `ModelProto`;
   - `ir_version` ≤ 9 and the default-domain opset ≤ 19;
   - operator domains come only from an allowlist: the default domain only (the spike found no other domain in any export);
   - no initializer or tensor uses external data;
   - there is exactly one graph input, float32 `[1, 3, S, S]`, where S is the record's Network_Input;
   - outputs satisfy the Device_Output_Contract with the record's `num_classes`.
4. `training_metadata.json` SHALL parse as JSON, and its `detection_arch`, `imgsz` / `resolution` and `num_classes` SHALL equal the record's. The record, not the metadata, is the source of class names, thresholds and geometry.
5. THE sha256 of the validated `model.onnx` SHALL be recorded as `conversion.onnx_sha256`, and SHALL equal the metadata's `onnx_sha256`.
6. IF validation fails, THEN Conversion_Status and `status` SHALL become `Failed`, with a `failure_reason` naming the violated rule and quoting the offending value. Nothing SHALL be packaged or published.
7. THE validator SHALL be a pure function in the shared layer (no AWS calls), unit-tested with hostile fixtures: a symlink, a hard link, `../`, an absolute path, an extra member, an oversize member, external-data ONNX, a custom-domain op, a wrong input shape, an embedded-NMS output, a segmentation output, opset 20, and IR version 10.

### Requirement 9: Packaging and publishing

**User Story:** As a Data Scientist, I want the converted model packaged and published like a portal-trained detector, so that it deploys to any Jetson through the path already proven on hardware.

#### Acceptance Criteria

1. A new predicate `is_detector_conversion_record` SHALL match `source = 'imported'`, `model_type = 'object_detection'`, `runtime = 'onnx'` with a `conversion` block. It SHALL route Conversion_Records in `packaging.package_components` before `is_onnx_import`, and in the bypass of `compilation.start_compilation_job`. `is_trained_detection_record` SHALL remain unchanged; imports still never match it.
2. After Requirement 8 passes, packaging SHALL call `package_trained_detection_component`, the same packager portal-trained records use. The device manifest SHALL be byte-identical to that of a portal-trained record with the same Detection_Record_Fields, pinned by a parity test.
   - That packager gained one step, from the trainer IR-version bugfix found by this spike: it lowers an IR > 9 header to what the graph needs.
   - The step is a no-op for Conversion_Artifacts, which are IR ≤ 9 by Requirement 6.6 and 8.3.
3. THE default targets SHALL be `jetson-xavier-jp5`, `jetson-xavier-jp6`, `jetson-xavier-jp7` and `x86_64-cpu`.
4. On success, packaging SHALL set Conversion_Status and `status` to `Completed` and `progress` to 100 with a conditional write, and record `packaged_components`. When invoked by the finalize path, it SHALL also trigger component creation (`_trigger_component_creation`), as the ONNX Smart Import auto-publish does today.
5. Provenance SHALL be preserved. The record stays `source = 'imported'`, Model Detail shows it as Imported, and it is not offered under "My trained detectors".
6. The record SHALL remain selectable as a base model exactly as today through `metadata.fine_tunable`, whatever its Conversion_Status.
7. A manual Package request on a Conversion_Record SHALL return HTTP 400 "conversion still running" while Conversion_Status is `InProgress`, and HTTP 400 carrying the failure reason when it is `Failed`.

### Requirement 10: Smart Import UI

**User Story:** As a Data Scientist, I want the Smart Import page to show me what my checkpoint is and pre-fill everything it can, so that I cannot mis-type class order or geometry.

#### Acceptance Criteria

1. Step 1 SHALL offer "Upload a file" (Requirement 3) beside the S3 URI field. It SHALL show upload progress, and use the returned `model_s3_uri` for inspect and convert.
2. For a checkpoint, a Checkpoint panel SHALL show:
   - the family (Ultralytics YOLO / RF-DETR), the task, and the library version that saved it;
   - the class count, and the class names in index order;
   - the training input size;
   - the verdict: either "Can be converted to ONNX" or every reason it cannot.
3. For a Convertible_Checkpoint:
   - the model type SHALL be locked to Object Detection, the output to ONNX, and the detection architecture to the probed family;
   - class names SHALL be pre-filled in index order with the count locked (renaming allowed);
   - the Network_Input SHALL be pre-filled from the training size, with the arch's bounds;
   - thresholds SHALL default per arch: YOLO score 0.25 and IoU 0.45; RF-DETR score 0.5 with no IoU field;
   - the geometry SHALL be shown read-only with its reason: YOLO letterbox, or RF-DETR square resize with ImageNet normalisation;
   - no Neo compilation targets SHALL be shown.
4. For a non-convertible checkpoint, the ONNX conversion SHALL be disabled with its reasons shown. The existing PyTorch "import as base model only" path SHALL remain available for fine-tunable kinds.
5. On submit, the page SHALL NOT call packaging itself (the server finalizes), and SHALL navigate to the record.
6. HTTP 400 and 503 messages SHALL be shown verbatim.

### Requirement 11: Conversion status UI

**User Story:** As a Data Scientist, I want the record page to show conversion progress, failure reasons and provenance, so that I know when the model is deployable and what exactly was converted.

#### Acceptance Criteria

1. Training Detail SHALL label a Conversion_Record by its state:
   - "Converting to ONNX" (`InProgress`);
   - "Validating and packaging" (`Finalizing`);
   - "Completed", with its packaged components;
   - "Conversion failed", with the reason, under an alert headed "Conversion failed" rather than "Training Job Failed".
2. WHILE a Conversion_Record's `status` is `InProgress`, Training Detail SHALL refetch it at least every 15 s until it reaches a terminal state. Today the poll reads a stale closure and never refetches.
3. THE Import Metadata panel SHALL show:
   - the source checkpoint: URI, sha256, size and saving library version;
   - the exporter: library, version and image digest;
   - after completion: the ONNX sha256, opset, IR version, input and output shapes, and the Parity_Check maxima.
4. CompilationTab and Model Detail SHALL treat a Conversion_Record like a detection ONNX package (no Neo compilation), and SHALL keep showing the fine-tunable badge as today.

### Requirement 12: Preservation, security gates and tests

**User Story:** As the team, we want this feature to leave every existing import, training and packaging path exactly as it is and to pass the repo's security gates, so that it can ship without regressions.

#### Acceptance Criteria

1. These SHALL stay unchanged, with their suites passing unmodified:
   - the ONNX Smart Import path (`test_model_converter_preserve_aspect.py` and the `test_onnx_jetson_*` suites);
   - the legacy `.pt` Smart Import and its sidecar (`test_model_converter_fine_tunable.py`, `test_model_import_fine_tunable.py`);
   - portal-trained detection create, package and parity (`test_detection_training_*`, `test_detection_manifest_parity.py`);
   - the probe (`test_checkpoint_probe.py`);
   - the trainers' static suites;
   - vLLM packaging;
   - base-model selection (`detectionBaseModels.test.ts`).
2. New tests SHALL cover every criterion of Requirements 2–11:
   - probe-to-assessment mapping;
   - upload-url;
   - the convert path: isolation flag, no sourcedir, the input channel, the record shape, and the 400 and 503 cases;
   - reducer transitions, including races and terminal states;
   - the validator's hostile fixtures;
   - the packaging branch and manifest parity;
   - the export entry point's static suite (libraries stubbed);
   - the frontend Smart Import and Training Detail changes.
3. THE sha256 of `model_converter.py` and `packaging.py` SHALL be rebaselined in `iam_out_of_scope_baseline.json`, with a note naming this spec. The cdk.out drift guards SHALL be run after moving `edge-cv-portal/infrastructure/cdk.out` aside.
4. New IAM (the ECR repository policy, any ECR pull grants on `DDASageMakerExecutionRole`, and the Packaging invoke grants for the two status writers) SHALL be listed with attribution in `iam_post_fix_approved_additions.json`. No new statement SHALL use a wildcard resource except for actions `iam_audit` classes as unscopable. `iam_audit`, `repo_audit` and the secrets audit SHALL be green; no new portal code references `pickle` or `torch.load`.
5. No LocalServer component build SHALL be required. The portal deploy SHALL follow `.kiro/steering/builds.md`: no component build running concurrently, and the guards green first.

### Requirement 13: On-device verification

**User Story:** As the team, we want the converted PPE model proven on a real Jetson, so that "deployable" means detections on hardware rather than green unit tests.

#### Acceptance Criteria

1. AFTER deploy, the PPE checkpoint SHALL be taken through the deployed portal UI end to end: uploaded from the browser, converted, packaged, published, and deployed to `jetson-thor1` (JP7).
2. With the checkpoint's own `sample_image.jpg` pinned to the Static_Image_Camera:
   - a workflow running the converted model SHALL return detections labelled only from `helmet, human, no-helmet, vest`;
   - those boxes and confidences SHALL match ultralytics' own `predict` on the same image (run off-device with the source checkpoint), within tolerances recorded in the verification notes;
   - the backend SHALL stay healthy over at least 10 runs, with no restart.
3. THE same component SHALL be verified on at least one JP5 or JP6 device if one is online. Otherwise the notes SHALL state that JP5 and JP6 are covered only by the in-job Fleet_Floor_Runtime check.
4. THE `best.pt` from `blue-plate-yolo-ft-v3-20260922-172408`, re-imported through this path, SHALL produce the same detections on `jetson-thor1`'s pinned blue-plate frame as its portal-trained component (recorded 2026-09-22 as 0.769 / 0.932 / 0.915, 3/3 plates): the trainer-parity oracle, checked on hardware.
5. THE results SHALL be recorded in `.kiro/specs/detector-checkpoint-import/verification-notes.md`, and the previously pinned static image restored.
