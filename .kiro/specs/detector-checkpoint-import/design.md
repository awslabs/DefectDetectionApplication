# Design Document

## Overview

Smart Import gains one new path. Given a detector checkpoint and an ONNX output, it:

1. classifies the checkpoint without executing it;
2. starts a network-isolated SageMaker job that loads and exports it;
3. validates everything that job produced as untrusted;
4. packages and publishes the result through the portal-trained detection packager, unchanged.

The imported record keeps today's fine-tunable sidecar, so it is both a base model and a deployable.

```
SmartImport.tsx
  ├─ POST /models/upload-url ─────────────▶ presigned PUT → s3://<uc bucket>/model-uploads/<uuid>/<file>
  ├─ POST /models/inspect ────────────────▶ model_converter: HeadObject cap → download → classify_checkpoint
  │                                          → assess_checkpoint()  (Checkpoint panel, pre-fill)
  └─ POST /models/convert (onnx) ─────────▶ model_converter:
         re-probe → assess → validate_conversion_request()
         sidecar converted-models/<name>-<hex>/checkpoint.pt  (exactly as today) + sha256
         create_training_job(build_conversion_job_request())   EnableNetworkIsolation=True
         put_item(build_conversion_record())                    status InProgress, conversion.status InProgress
                                   │
                ┌──────────────────▼───────────────────────────────────────┐
                │ Conversion_Job (use-case account, no network, no creds)  │
                │ Export_Image: export_checkpoint.py                        │
                │  sha256(input) == EXPECTED → load (unpickle) → task gate  │
                │  → export (one-to-many, fp32, static, opset 17)           │
                │  → contract check → ORT 1.16.3 load+run → parity          │
                │  → /opt/ml/model/{model.onnx, training_metadata.json}     │
                │  on any failure: /opt/ml/output/failure ("FATAL: …")      │
                └──────────────────┬───────────────────────────────────────┘
      SageMaker state change        │
  training_events.py (EventBridge) ─┼─▶ reduce_conversion() ─ Failed/Stopped → Failed (FailureReason verbatim)
  training.get_training_job (GET) ──┘                        └ Completed → claim InProgress→Finalizing (conditional)
                                                                 └ winner: async invoke Packaging {finalize_conversion}
packaging.package_components:
  is_detector_conversion_record → validate_conversion_artifact() (tar members, ONNX structure vs record)
    → package_trained_detection_component()   (UNCHANGED)
    → Finalizing→Completed (conditional) → _trigger_component_creation()  → model-<name> 1.0.0 on jp5/jp6/jp7/x86
```

## Decisions

**D1: Unpickle only inside an isolated SageMaker job, never in a portal Lambda.** Every portal Lambda shares `createLambdaRole` (compute-stack.ts). That role has read-write on about 20 tables, including the Portal_Identity registry, `iam:PassRole` on `DDA*Role`, and `sts:AssumeRole` into use-case accounts. Code running there is catastrophic.

Rejected alternatives:
- A zip Lambda: torch does not fit, which is also why `inspect_pytorch_model` is dead code today.
- A container Lambda: its credentials are in the environment and it has internet egress unless it is VPC-isolated.
- A job with `EnableNetworkIsolation=False` (the trainers' posture): the pickle runs with egress and with `DDASageMakerExecutionRole` credentials, which read and write every `dda-*` bucket.

With isolation, the container has no network and no credentials. SageMaker itself stages the input and uploads `/opt/ml/model`. At worst a malicious checkpoint produces a bad artifact, which D6 catches.

**D2: A dedicated, pinned Export_Image with the code baked in.** Network isolation rules out the script-mode pattern: the toolkit downloads `sagemaker_submit_directory` from inside the container and pip-installs `requirements.txt`, and both need network. The build follows the `plugin-build-images` precedent:
- a CDK-created ECR repository;
- an out-of-band `build-and-push.sh`;
- the image URI passed by CDK context (`-c detectorExportImage=<repo>@sha256:<digest>`, the `detectionTrainingImage` precedent), so the record carries the digest with no extra IAM.

A `DockerImageAsset` (the Grounded-SAM worker precedent) was rejected because it lands in the CDK bootstrap repository, whose policy cannot grant use-case accounts a pull.

**D3: Emit the trainers' artifact shape and reuse the proven packager.** The job writes `model.onnx` and `training_metadata.json` with the keys `package_trained_detection_component` reads. That packager was verified on `jetson-thor1` for both YOLO and RF-DETR on 2026-09-22 (`docs/detection-training-gap.md`), so no device-side change is needed.

The record keeps `source='imported'` for provenance (licensing matters: the PPE weights are AGPL-3.0). A new predicate routes it, rather than disguising it as a trained record. Disguising it would make `is_trained_detection_record` match, list it under "My trained detectors", and hide the provenance.

**D4: The exporter pins its own ultralytics version and proves parity with the trainers.** Checkpoints are not forward-compatible: old code loading new pickles can fail. The PPE checkpoint was saved by 8.4.2, and YOLO26 needs ≥ 8.4, while the trainers pin 8.3.40.

The Export_Image therefore pins the newest release the spike validates. Equivalence with the trainers is proven by an oracle rather than by a shared version: the `best.pt` of portal job `blue-plate-yolo-ft-v3` (8.3.40) must export to a graph numerically equivalent to that job's own `model.onnx`, and must reproduce that component's on-device detections.

Export arguments must select the raw one-to-many output. On ultralytics ≥ 8.4 that is `nms=None`. `nms=False`, which `train.py` passes, means "YOLO26 one-to-one head" there and yields `(N, 300, 6)`. The device decoder treats that shape as anchors with 6 channels and decodes it silently wrong.

**D5: Check against the fleet floor, twice.** Packaging fans out to jp5, jp6, jp7 and x86_64-cpu. The oldest runtime among them is onnxruntime 1.16.3, which supports opset ≤ 19 and IR ≤ 9. The trainers' exports have only ever been verified on JP7's 1.23.2.

1. The job loads and runs the graph in a separate `/opt/ort-floor` venv with `onnxruntime==1.16.3`. Two ORT versions cannot share one environment.
2. The portal validator independently checks the IR version and opset (D6).

**D6: The job's output is attacker-controlled.** Before packaging, a pure validator:
- reads only regular-file tar members with exact names. `package_trained_detection_component` calls `extractall`, and a symlink `model.onnx → /proc/self/environ` would copy the Packaging Lambda's credentials into a published component;
- parses the ONNX structure from an mmap;
- compares everything against the record.

The record, which the portal wrote from its own probe and the user's confirmation, is the source of truth for class names, thresholds and geometry. The job's metadata must agree with it or the conversion fails.

**D7: A conversion-aware reducer inside the two existing status writers.** Setting `training_job_name` gives both writers, and the Logs tab, for free. But their blind copy of SageMaker's status would overwrite a portal-side `Failed` with SageMaker's `Completed` on the next GET. Both writers therefore branch on the `conversion` block into one pure reducer.

Every write is conditional on the expected prior `conversion.status`. The `InProgress → Finalizing` claim is the exactly-once gate for the finalize invoke.

Rejected alternatives:
- A separate poller or Step Function: new infrastructure to duplicate what the two writers already observe.
- Leaving finalization manual (like trained detectors): the record would read `Completed` before validation.

**D8: Geometry is derived, not chosen.** YOLO letterboxes (ultralytics trains letterboxed; squashing measured a 1.35× mean confidence loss). RF-DETR square-resizes and normalises. `build_detection_device_manifest` already encodes both, so the request cannot contradict them.

**D9: Pre-flight is a hint; the job is the authority.** The probe's GLOBAL and `train_args` reading gives fast UX and cheap rejection of obvious non-detectors. The job re-checks everything with the real libraries: `model.task`, the head class, class count, and the output layout.

**D10: Uploads use a server-issued staging key.** This follows the static-image pin precedent (`camera_registry.get_static_image_upload_url`). The generic `data/upload-url` route was rejected because the caller chooses bucket and key there.

## Components

### 1. Shared layer: `detector_conversion.py` (new, pure, stdlib)

`edge-cv-portal/backend/layers/shared/python/detector_conversion.py`, re-exporting nothing torch-related.

```python
FLEET_FLOOR_ORT = '1.16.3'; FLEET_MAX_OPSET = 19; FLEET_MAX_IR = 9
CONVERSION_OPSET = 17
CHECKPOINT_SIZE_CAP = 512 << 20        # decided by the spike (Req 1.3(f)): 1 GiB measured 27 s
ARTIFACT_SIZE_CAP = 2 << 30
CONVERSION_INSTANCE = 'ml.m5.xlarge'; CONVERSION_VOLUME_GB = 30; CONVERSION_MAX_RUNTIME_S = 1800
ACCEPTED_YOLO_HEADS = ('ultralytics.nn.modules.head.Detect',             # YOLOv8/11/26
                       'ultralytics.nn.modules.head.v10Detect')          # spike: one-to-many exports
RFDETR_SIZES = {'nano': 384, 'small': 512, 'medium': 576, 'large': 704}  # native-only (spike)
REJECTED_ULTRALYTICS_MODELS = {'SegmentationModel': 'segmentation', 'PoseModel': 'pose',
    'OBBModel': 'oriented boxes', 'ClassificationModel': 'classification',
    'WorldModel': 'open-vocabulary', 'YOLOEModel': 'open-vocabulary',
    'RTDETRDetectionModel': 'ultralytics RT-DETR'}
CONVERSION_STATUSES = ('InProgress', 'Finalizing', 'Completed', 'Failed')

def assess_checkpoint(probe: dict) -> CheckpointAssessment      # Req 2.3-2.6
def validate_conversion_request(body, assessment) -> ConversionParams   # Req 4.3-4.5 (ValueError -> 400)
def build_conversion_job_request(*, job_name, image, role_arn, input_prefix_s3,
                                 output_s3, params, source_sha256, tags) -> dict  # Req 5
def build_conversion_record(*, training_id, usecase_id, user, params, assessment,
                            fine_tunable, job, source) -> dict                     # Req 4.6
def is_detector_conversion_record(record) -> bool                                 # Req 9.1
def plan_conversion_transition(record, sm_status, sm_failure_reason, sm_artifact_s3)
        -> Optional[Transition]                                                    # Req 7 (pure)
def apply_conversion_transition(table, training_id, transition) -> bool           # conditional write
def validate_conversion_artifact(tar_path, record, workdir) -> ValidatedArtifact   # Req 8 (pure, no AWS)
```

`assess_checkpoint` maps the probe's `kind`, `evidence.model_class`, `evidence.pickle_globals`, `evidence.train_args`, `evidence.version`, `num_classes` and `class_names` onto the Requirement 2 schema.

- **Ultralytics head classes** come from `pickle_globals` under `ultralytics.nn.modules.head.*`.
- **Train input size** is `train_args.imgsz` (YOLO) or `evidence.args_picks.resolution` (RF-DETR).
- **RF-DETR size inference** uses the mapping *decided by the spike*: `model_name`, then `args.encoder`/`resolution`/head shapes.
- **Reproducibility:** for the PPE file the probe already returns `model_class = ultralytics.nn.tasks.DetectionModel`, head `Detect`, `train_args = {task: detect, imgsz: 640, model: yolo11n.pt}`, `version = 8.4.2`, and names `[helmet, human, no-helmet, vest]`. That output is captured as a test fixture.

`validate_conversion_artifact`:

1. Opens the tar with `tarfile.open(..., 'r:gz')` and iterates `TarInfo` headers.
2. Allows only regular files named `model.onnx` / `training_metadata.json`, plus at most one `.`/`./` directory entry. Each member must be within its cap and appear once.
3. Streams exactly those two members via `extractfile` into `workdir`, hashing as it goes.
4. Parses the ONNX `ModelProto` from an `mmap`, skipping initializer payloads by length:
   - field 1 `ir_version`;
   - field 8 `opset_import`;
   - field 7 graph: nodes' domains, initializers' `data_location`/`external_data`, inputs, outputs;
   - field 25 functions' domains.
5. Checks Requirement 8.3 and 8.4 against the record.

The varint and wire-skipping helpers are lifted from `checkpoint_probe.py` (`_varint`, `_read_varint`, `_skip_wire`, `_parse_value_info`) into this module, not imported. That keeps the probe unchanged, and the preservation suite pins `test_checkpoint_probe.py`. Memory use is proportional to graph metadata, not weights.

### 2. Export entry point: `datasets/detection_training/export_checkpoint.py` (new)

This file is a sibling of the trainers so that it can import `train_rfdetr`'s pure functions (`export_onnx`, `read_onnx_io`, `verify_input`, `verify_two_outputs`, `checkpoint_num_classes`, `checkpoint_class_names`) and `_common.write_metadata`. It does not import `train.py`, which resolves `IMGSZ`/`OPSET` from the environment at import. `train.py` is unchanged; its export kwargs are mirrored here and the trainer-parity oracle proves the equivalence.

```python
def main():
    try:
        cfg = read_env()                       # DETECTION_ARCH, NETWORK_INPUT, ONNX_OPSET, EXPECTED_NUM_CLASSES,
                                               # EXPECTED_SHA256, RFDETR_SIZE?, PARITY_* — all required but RFDETR_SIZE
        src = single_input_file('/opt/ml/input/data/checkpoint')   # FATAL unless exactly one file
        require_sha256(src, cfg.expected_sha256)
        if cfg.arch == 'yolo':
            disable_autoupdate()               # before `import ultralytics` (Req 5.7)
            model = load_yolo(src)             # YOLO(str(src)); FATAL on failure, message names the saving version
            gate_yolo(model, cfg)              # task == 'detect', head class accepted, nc == EXPECTED_NUM_CLASSES
            onnx_path = export_yolo(model, cfg)   # imgsz=S, opset=17, dynamic=False, simplify=True, half=False,
                                                  # + the spike-decided one-to-many args (nms=None on >=8.4)
            outs = verify_yolo_contract(onnx_path, cfg)   # 1 input [1,3,S,S] f32; 1 output [1,4+C,N] | [1,N,4+C], 4+C<N
        else:
            model = load_rfdetr(src, cfg)      # size inferred/given; strict key/shape check (Req 6.1)
            onnx_path = export_onnx(model, cfg_like, WORK/'export')          # trainer's function
            outs = verify_rfdetr_contract(onnx_path, cfg)                   # trainer's verify_input/verify_two_outputs
        floor = run_on_fleet_floor(onnx_path)  # subprocess /opt/ort-floor/bin/python: load + run, finite, ir/opset
        parity = parity_check(model, onnx_path, cfg)   # seeded tensor + bundled image; FATAL beyond tolerance
        write_artifact(onnx_path, build_conversion_metadata(cfg, outs, floor, parity))
    except SystemExit as e:
        write_failure(str(e.code)); raise        # /opt/ml/output/failure (Req 5.6)
    except Exception as e:
        write_failure(f"FATAL: {type(e).__name__}: {e}"); raise SystemExit(1)
```

Both parity inputs are deterministic: a seeded `[1, 3, S, S]` uniform tensor, and ultralytics' bundled `ultralytics/assets/bus.jpg` letterboxed (YOLO) or square-resized and normalised (RF-DETR) exactly as the device does (`basic_preprocessor.py`). The reference is the source model in the exporter's mode:
- **YOLO:** the fused `DetectionModel` with the head in export mode and the one-to-many branch selected, which the spike pins for 8.4.
- **RF-DETR:** the model's export forward.

Every `sys.exit` message keeps the trainers' `FATAL:` convention. SageMaker surfaces the first 1,024 characters of `/opt/ml/output/failure` as `FailureReason`.

### 3. Export image: `edge-cv-portal/detector-export-image/` (new)

```
detector-export-image/
├── Dockerfile            # FROM public.ecr.aws/docker/library/python:3.11-slim-bookworm@sha256:<digest>
│                         # (bookworm: glibc >= 2.41 refuses the ORT 1.16.3 wheel's executable-stack .so)
├── requirements.lock     # exact pins: torch/torchvision CPU (2.5.1, trainer parity unless the spike changes it),
│                         # ultralytics, rfdetr[onnx], onnx, onnxslim, onnxruntime, numpy<2, opencv-python-headless
├── ort-floor.lock        # onnxruntime==1.16.3, numpy<2   → /opt/ort-floor venv
└── build-and-push.sh     # stages a minimal context, buildx --platform linux/amd64 --provenance=false,
                          # pushes <acct>.dkr.ecr.<region>.amazonaws.com/dda-detector-export:<tag>, prints the @sha256 URI
```

- **Contents:** the `Dockerfile` COPYs `export_checkpoint.py`, `train_rfdetr.py` and `_common.py` into `/opt/program`.
- **Entrypoint:** `ENTRYPOINT ["python3", "/opt/program/export_checkpoint.py"]`. SageMaker's `train` argument is ignored.
- **Environment:** it sets `YOLO_AUTOINSTALL=False` and `YOLO_OFFLINE=True`.
- **Build context:** the script stages only these files, because the repository root is far too large to send as a build context.
- **Architecture:** `ml.m5` instances are x86_64, and the dev host is x86_64.
- **Base image:** Python 3.11 matches the trainers' DLC and has onnxruntime 1.16.3 wheels.
- **Audits:** `docker_base_image_audit.py` does not scan this Dockerfile today, but the image follows its policy anyway: an ECR-hosted base pinned by digest.

### 4. Infrastructure (`edge-cv-portal/infrastructure/lib/`)

`compute-stack.ts`:

- `new ecr.Repository(this, 'DetectorExportRepository', {repositoryName: 'dda-detector-export', imageScanOnPush: true, imageTagMutability: ecr.TagMutability.IMMUTABLE, removalPolicy: RETAIN})`.
- A repository policy allowing `ecr:BatchGetImage`, `ecr:GetDownloadUrlForLayer` and `ecr:BatchCheckLayerAvailability` to `arn:aws:iam::<id>:role/DDASageMakerExecutionRole`, for each `trustedUseCaseAccountIds` id other than the portal account. The ids are concrete: `iam_audit` rejects `arn:aws:iam::*:role/`.
- `-c detectorExportImage=<uri>` sets `DETECTOR_EXPORT_IMAGE` on `ModelConverterHandler`. When it is absent the variable is empty, and Requirement 4.8 returns 503.
- `trainingHandler.addEnvironment('PACKAGING_FUNCTION_NAME', …)` and the same on `TrainingEventsHandler`, plus `packagingHandler.grantInvoke(trainingHandler)` and `packagingHandler.grantInvoke(trainingEventsHandler)`.

`api-gateway-stack.ts`, and its duplicate `api-model-stack.ts`: add `POST /models/upload-url` → `ModelConverterHandler`, Cognito-authorized like its siblings.

`usecase-account-stack.ts`: add ECR pull on the repository ARN to `DDASageMakerExecutionRole` only if the spike shows the CDK role lacks it (Requirement 1.3(g)). `ecr:GetAuthorizationToken` is on `*` and is in `iam_audit`'s unscopable list. The out-of-band role from `deploy-account-role.sh` already has ECR pull on `*`.

**Region:** SageMaker training images must be in the job's region. Version 1 supports the image's region only. Convert returns 503 "checkpoint conversion is not configured for region X" for a use case elsewhere. ECR cross-region replication is a follow-up.

### 5. `model_converter.py` (preservation-tracked; rebaselined)

- **`inspect_model_endpoint`:**
  - `HeadObject` enforces the cap (Requirement 2.7).
  - For `.pt`/`.pth`, `classify_checkpoint` then `assess_checkpoint` replaces the dead `inspect_pytorch_model` call on this route.
  - The response keeps `inspection_result.type`, `suggested_type`, `detection_arch`, `num_classes`, `input_width/height` and `architecture_hints`, so today's pre-fill code keeps working, and gains `checkpoint` and `class_names`.
  - ONNX sources are untouched.
- **`get_model_upload_url` (new route):** role check, extension and size check, server-issued key, a SigV4 presign without Content-Type (15 min), and `ensure_bucket_cors`. That helper is moved from `data_management.py` to the shared layer, and `data_management.py` re-imports it unchanged.
- **`convert_model`:** a new branch that runs *before* `generate_dda_package` when the source is `.pt`/`.pth` and `export_format == 'onnx'`:
  1. re-download and re-probe, `assess_checkpoint`, 400 if not convertible;
  2. `validate_conversion_request`;
  3. 503 if `DETECTOR_EXPORT_IMAGE` is empty or its region differs;
  4. the sidecar via the *existing* code, and its sha256;
  5. a SageMaker client from the same assumed use-case credentials;
  6. `create_training_job(**build_conversion_job_request(...))`;
  7. `put_item(build_conversion_record(...))`;
  8. `log_audit_event('convert_checkpoint', …)` with the source URI, source sha256, job name and image digest. The ONNX sha256 is added by the finalize audit event;
  9. 200.

  The `'pytorch'` branch and the ONNX branch are textually unchanged. No `torch`, `pickle` or `torch.load` appears in the new code (`repo_audit` scans this file).

### 6. Lifecycle hooks

`training_events.handle_training_state_change`: after locating the record,

```python
if detector_conversion.is_detector_conversion_record(record):
    t = plan_conversion_transition(record, status, detail.get('FailureReason'),
                                   (detail.get('ModelArtifacts') or {}).get('S3ModelArtifacts'))
    if t and apply_conversion_transition(table, training_id, t) and t.invoke_finalize:
        invoke_packaging_finalize(training_id)        # async, InvocationType='Event'
    return {...}                                       # never the generic status copy / auto-compile
```

`training.get_training_job` inserts the same branch before its generic `if status != job.get('status')` block. It then returns the post-transition record (re-read, or patched in memory). Records without `conversion` take exactly today's code.

`plan_conversion_transition` implements this table. It is a pure function, fuzzed with Hypothesis:

| conversion.status | SageMaker status | Transition |
|---|---|---|
| InProgress | InProgress / Starting / Downloading… | none |
| InProgress | Completed (artifact present) | → Finalizing, `artifact_s3`, progress 80, **invoke_finalize** |
| InProgress | Completed (no artifact) | → Failed, "conversion job completed without an artifact" |
| InProgress | Failed / Stopped | → Failed, `failure_reason = FailureReason` verbatim, progress 0 |
| Finalizing / Completed / Failed | anything | none (Finalizing is left to packaging; terminal never overwritten) |

`apply_conversion_transition` issues an `update_item` with `ConditionExpression = #conv.#st = :from`. It treats `ConditionalCheckFailedException` as `False`, because another writer won the race.

### 7. `packaging.py` (preservation-tracked; rebaselined) and `compilation.py`

In `package_components`, a new block comes after the vLLM branch and before `is_trained_detection_record`. The trained-record and ONNX-import blocks stay textually identical.

```python
if is_detector_conversion_record(training_job):
    conv = training_job['conversion']['status']
    if conv == 'InProgress':  return 400 "conversion still running"
    if conv == 'Failed':      return 400 failure_reason
    # Finalizing (normal / retry, Req 7.9) or Completed (re-package)
    head_object → cap; download artifact; validated = validate_conversion_artifact(...)
        except ConversionValidationError as e: apply Finalizing→Failed(e); return 422
    component_s3 = package_trained_detection_component(artifact_s3, training_job, s3, usecase)   # the trained packager
    # (its fleet-floor IR header step, added by the trainer IR bugfix, is a no-op for IR <= 9 artifacts)
    apply Finalizing→Completed {status Completed, progress 100, packaged_components,
                                conversion.onnx_sha256, conversion.onnx_summary, conversion.completed_at}
    if body.get('auto_triggered') or body.get('finalize_conversion'): _trigger_component_creation(...)
```

`package_trained_detection_component` downloads the artifact again and calls `extractall`. That is safe here: validation has just proven the tarball holds exactly two regular files. The object is written once by SageMaker, and any principal able to swap it inside that window can already publish components directly.

In `compilation.start_compilation_job`, the bypass condition becomes `_is_onnx_import(job) or is_trained_detection_record(job) or is_detector_conversion_record(job)`.

### 8. Frontend (`edge-cv-portal/frontend/src/`)

- **`services/api.ts`:**
  - `getModelUploadUrl()`;
  - `inspectModel` gains `checkpoint` and `class_names` in its response;
  - `convertModel`'s response becomes a union carrying `status` and `conversion`;
  - `TrainingJobRecord` gains `conversion`.
- **`utils/detectorConversion.ts`:** `isDetectorConversionRecord`, `conversionStatusLabel`, `conversionLocksFor(assessment)` (which fields are locked and their values), and `validateClassNames(names, count)`.
- **`pages/SmartImport.tsx`:**
  - Step 1 has an Upload/S3-URI toggle. The upload is an `XMLHttpRequest` PUT of the `File` (fetch has no upload-progress events) with no Content-Type signing concern, followed by inspect.
  - The Checkpoint panel.
  - For a Convertible_Checkpoint: the locks and pre-fill from `conversionLocksFor`, the class-name editor with its count locked, read-only geometry, and no compilation targets.
  - Submit calls `convertModel` and, on a `conversion` response, navigates to `/training/<id>` without calling `startPackaging`.
- **`pages/TrainingDetail.tsx`:**
  - status labels and the "Conversion failed" alert;
  - a polling fix: the interval callback reads the latest status through a ref or functional state instead of the mount-time `job`, and the interval is 15 s while `InProgress`;
  - the Import Metadata additions from `conversion`.
- **`components/CompilationTab.tsx`:** `isDetectorConversionRecord` joins the detection ONNX branch.
- **`pages/ModelDetail.tsx`:** unchanged, because the fine-tunable badge already keys on `metadata.fine_tunable`.

## Data model

```jsonc
// TrainingJobs item (Conversion_Record), after completion
{
  "training_id": "…", "usecase_id": "…", "model_name": "ppe-detection", "model_version": "1.0.0",
  "source": "imported", "model_type": "object_detection", "runtime": "onnx",
  "status": "Completed", "progress": 100,
  "training_job_name": "ppe_detection-cnv-20261001120000", "training_job_arn": "arn:aws:sagemaker:…",
  "instance_type": "ml.m5.xlarge", "algorithm_uri": "<acct>.dkr.ecr.us-east-1.amazonaws.com/dda-detector-export@sha256:…",
  "artifact_s3": "s3://<bucket>/models/conversion/<job>/<job>/output/model.tar.gz",
  "detection": { "detection_arch": "yolo", "network_input_width": 640, "network_input_height": 640,
                 "class_names": ["helmet","human","no-helmet","vest"], "num_classes": 4,
                 "score_threshold": 0.25, "iou_threshold": 0.45, "preserve_aspect": true, "onnx_opset": 17 },
  "metadata": { "framework": "PYTORCH", "framework_version": "ultralytics 8.4.2", "model_file": "checkpoint.pt",
                "model_type": "object_detection", "image_width": 640, "image_height": 640,
                "input_shape": [1,3,640,640],
                "fine_tunable": { "arch": "yolo", "kind": "ultralytics_checkpoint",
                                  "checkpoint_s3": "s3://<bucket>/converted-models/ppe_detection-1a2b3c4d/checkpoint.pt",
                                  "class_names": ["helmet","human","no-helmet","vest"], "num_classes": 4 } },
  "conversion": { "status": "Completed", "job_name": "…", "export_image": "…@sha256:…",
                  "source_s3": "s3://<bucket>/model-uploads/<uuid>/best.pt",
                  "source_sha256": "a00b6fce…2119", "source_bytes": 5475290,
                  "onnx_sha256": "…", "onnx_summary": { "ir_version": 8, "opset": 17,
                     "input": [1,3,640,640], "outputs": [[1,8,8400]], "exporter": "ultralytics <pin>",
                     "parity": { "max_abs": …, "max_rel": … } },
                  "started_at": 1790000000000, "completed_at": 1790000300000 },
  "packaged_components": [ { "target": "jetson-xavier-jp7", "component_package_s3": "…", "status": "packaged" }, … ]
}
```

`training_metadata.json`, as written by the job for YOLO:
- `detection_arch: "yolo"`, `imgsz`, `num_classes`, `class_names`, `onnx_output_shape`;
- `device_manifest_hints: {layout, network_input, preserve_aspect: true, iou_threshold, score_threshold}`;
- `opset`, `ir_version`, `exporter`, `source_sha256`, `onnx_sha256`, `parity`, `fleet_floor: {ort, loaded, finite}`.

For RF-DETR it writes the trainer's keys (`resolution`, `onnx_output_shapes`, `top_k`, `logits_slots`, `background_slot`, `rfdetr_size`) plus the same additions.

## Threat model

| Threat | Control |
|---|---|
| Malicious pickle executes code on load | Load happens only in the isolated job: no network, no credentials. Portal Lambdas classify with the literal-only probe (D1). |
| Exfiltration of this or other checkpoints | No network or credentials in the job. The input channel holds only this object. |
| Tampering with other data | No credentials. SageMaker writes only this job's output prefix. |
| Crafted tar (symlink, hardlink, traversal) makes the packager read host files such as Lambda credentials | Member allowlist of regular files before any extraction (Requirement 8.2). |
| Oversized artifact or member (DoS) | `HeadObject` cap and per-member caps. Job runtime and volume are bounded. |
| ONNX with external-data references or custom-domain ops | The validator rejects them (Requirement 8.3). |
| Lying metadata (size, classes) | The record is the source of truth; any mismatch sets Failed (Requirement 8.4). |
| Sidecar swapped between probe and job (TOCTOU) | sha256 is recorded at convert and verified in the job (Requirement 5.3). |
| Artifact swapped between validation and packaging | Only principals who can already publish components can write the prefix, so no privilege is gained. |
| Exporter dependency supply chain | Exact pins, digest-pinned base, ECR scan-on-push, image digest on every record. |
| Silent mis-decode on device (NMS-baked or one-to-one output, wrong task, fp16, class order) | Task gate plus contract check in the job and again in the portal. Class names come from the checkpoint in index order. fp32 is enforced. The device run on hardware is compared with ultralytics `predict` (Requirement 13.2). |
| Use-case bucket CORS widened | The same helper and policy as the existing data upload route; nothing broader. |

## Error handling

| Condition | Where | Result |
|---|---|---|
| Source > cap | inspect / convert | 400 `Checkpoint is <n> bytes; the limit is <cap>` |
| Not convertible (TorchScript, state_dict, seg / pose / obb / cls, World, RT-DETR, PML size, …) | inspect → `reasons`; convert → 400 | nothing created |
| Bad request field (S, thresholds, class names, preserve_aspect) | convert | 400 naming the field |
| No image configured, or the use case is in another region | convert | 503 |
| `CreateTrainingJob` fails | convert | 502 carrying SageMaker's message; no record |
| sha256 mismatch, several input files, load failure, wrong task or head, class count ≠ record | job | Failed, `FATAL: …` via `/opt/ml/output/failure` |
| Output not one-to-many, fp16, dynamic input, NMS baked in | job | Failed, `FATAL:` quoting the shapes and dtypes |
| Fleet-floor load or run fails, NaN or Inf | job | Failed, `FATAL: onnxruntime 1.16.3 …` |
| Parity beyond tolerance | job | Failed, `FATAL: parity …` with the maxima |
| Job Completed without an artifact | reducer | Failed |
| Artifact violates Requirement 8 | packaging (finalize) | Failed, naming the rule and value; 422 to the invoker |
| Finalize invoke lost | — | Record stays Finalizing; the Package action retries (Requirement 7.9) |
| Package while InProgress or Failed | packaging | 400 |

## Testing strategy

Commands follow the sibling specs. Backend tests run from `edge-cv-portal/backend` with `~/.venvs/dda-portal-tests/bin/python -m pytest tests/<file> -q -p no:cacheprovider`, on targeted files only. Frontend: `npx vitest run <file>` and `npm run build`. CDK: `npm test -- <file>` in `infrastructure/`.

- **`tests/test_detector_conversion_assessment.py`:** probe-output fixtures (the real PPE evidence, synthetic seg/pose/obb/cls/World/RT-DETR/v10 globals, RF-DETR R2 and R2′ evidence) mapped to `convertible`, `reasons` and the pre-fill.
- **`tests/test_detector_conversion_request.py`:** boundary validation. The job request: `EnableNetworkIsolation is True`; no `sagemaker_program` or `sagemaker_submit_directory`; a single input channel on the sidecar prefix *with* its trailing `/`; the bounds; the environment contract.
- **`tests/test_detector_conversion_reducer.py`:** the transition table, plus Hypothesis over event sequences and interleavings against a fake table with real conditional semantics. The properties: terminal states are never overwritten, and exactly one finalize claim is made.
- **`tests/test_detector_conversion_validator.py`:** synthetic tarballs and hand-assembled ONNX protobufs, so the portal venv stays onnx- and torch-free. Covers every hostile fixture in Requirement 8.7, plus the accepting cases (YOLO `[1,8,8400]`, RF-DETR `[1,300,4]` + `[1,300,5]`).
- **`tests/test_model_converter_checkpoint_conversion.py`** (moto, with SageMaker stubbed):
  - the inspect `checkpoint` block;
  - upload-url (key shape, TTL, extension and size 400s, role 403);
  - the convert happy path (sidecar bytes identical, one job, record shape);
  - the 400, 503 and 502 paths;
  - legacy `.pt` + pytorch, and ONNX sources, unchanged.
- **`tests/test_training_conversion_lifecycle.py`:** the EventBridge and GET branches, with no generic copy for conversion records. Non-conversion records keep today's behaviour, pinned against the existing expectations.
- **`tests/test_packaging_detector_conversion.py`:**
  - finalize → validate → package: the manifest is byte-equal to a trained record's with the same fields;
  - `_trigger_component_creation` is called once;
  - validation failure → Failed;
  - the InProgress and Failed 400s;
  - a Finalizing retry;
  - `compilation` bypass.
- **`tests/test_export_checkpoint_static.py`:** the entry point with ultralytics, rfdetr, onnxruntime and torch stubbed, following `test_train_yolo_static.py`:
  - the export kwargs (no `half`, `dynamic=False`, `simplify=True`, opset 17, the pinned one-to-many argument);
  - AutoUpdate disabled before import;
  - the sha256 gate;
  - the contract verifiers (pure);
  - the failure-file writer;
  - the metadata keys.
- **Frontend:** `SmartImport.checkpoint.test.tsx` (panel, locks, class-order rendering, submit payload, no packaging call), `TrainingDetail.conversion.test.tsx` (labels, alert header, polling refetches with fake timers) and `detectorConversion.test.ts`.
- **CDK:** `detector-export-infra.test.ts`:
  - the repository is immutable and scanned;
  - the policy principals are exactly the trusted accounts' `DDASageMakerExecutionRole`;
  - the environment is set only when the context is set;
  - the two invoke grants;
  - the upload-url route in both API stacks.
- **Real jobs** (spike, re-run as a gate after the image is final): the Requirement 1.1 fixture set through the actual job, asserting the expected verdicts. PPE and blue-plate pass; YOLO26 passes one-to-many; the seg checkpoint is rejected by the pre-flight and, if forced, by the job; v10 per the spike; RF-DETR per the spike.
- **Gates:**
  - the preservation suite at baseline, with `model_converter.py` and `packaging.py` rebaselined in the same change;
  - the cdk.out drift guards, after moving `cdk.out` aside;
  - the CDK-synth IAM baseline, with the new statements added to `iam_post_fix_approved_additions.json` (never regenerating the fixed baseline);
  - `iam_audit` and `repo_audit` green.
- **Hardware:** Requirement 13.

## Open questions (answered by the spike, Requirement 1)

The answers, with their evidence, are in `docs/detector-checkpoint-import-spike.md` (2026-09-25).

1. The ultralytics pin, and the exact export and reference-model incantation for one-to-many on 8.4.x, for both YOLO11 and YOLO26. **Answer:** 8.4.162 with `nms=None`. The reference is the `Exporter` instance's own traced `.model`.
2. Whether `v10Detect` can export one-to-many at all. **Answer: yes.** `nms=None` exports its trained one-to-many branch as `[1, 84, 8400]`, with the same detections as its one-to-one head on the bundled images, so `v10Detect` is accepted.
3. The rfdetr pin, the size-inference table, strict-load verification, and `positional_encoding_size` at a non-native resolution. **Answer:**
   - rfdetr 1.10.1.
   - The size comes from a unique exact key/shape match against each Apache-2.0 size shell, trying `model_name` first.
   - The strict re-check runs after loading.
   - Native resolution only: an explicit `resolution` re-derives `positional_encoding_size`.
4. Parity tolerances, and whether the fused-BN reference suffices or needs per-output tolerances. **Answer:**
   - YOLO is compared element-wise: box 0.1 px, score 1e-3.
   - RF-DETR needs a detection-level, permutation-invariant comparison: pairs ≥ 0.25 matched within 1e-3 / 5e-3, and at least 75 % of slots with a one-to-one partner. The reason is that onnxruntime 1.16.3 reorders near-tied tail queries.
   - Both the exporter's onnxruntime and the floor runtime are checked.
5. The size cap measured against the 29 s API Gateway integration timeout. **Answer: 512 MiB** (1 GiB measured 27 s). The probe's cost does not depend on file size, so no ranged-read change is needed.
6. Whether the CDK `DDASageMakerExecutionRole` can pull from the repository, and whether the cross-account policy works. **Answer:** the role already has ECR pull on `*`. Verified single-account only; no second account exists.
