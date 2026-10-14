# Design Document

## Overview

Object Detection becomes a fifth portal model type. The design follows one rule: **reuse the two things that already work and only add the glue between them.** The Detection_Entry_Point (`datasets/detection_training/train.py`) already trains letterboxed and exports a device-correct ONNX; the ONNX Compilation_Bypass in `compilation.py` / `packaging.py` / `CompilationTab.tsx` already carries an ONNX artifact to a Greengrass component without Neo. What is missing is (a) a way to launch the entry point from `training.py`, (b) a record shape downstream code can recognize, (c) a packager that turns the entry point's flat `model.onnx` into the nested component layout with a correct Device_Manifest, and (d) a frontend that offers the option and stops misclassifying bounding-box manifests as Ground Truth.

The trained-detection flow after this change:

```
CreateTraining.tsx ── POST /training {model_type: object_detection, hyperparameters}
        │
        ▼
training.py::create_training_job
   ├─ validate_detection_manifest()           ← shared/detection_training.py
   ├─ build sourcedir.tar.gz from bundled detection_training/ → s3://{uc}/models/detection-training/{job}/
   ├─ sagemaker.create_training_job(TrainingImage=DLC, script mode, Environment=MANIFEST_S3…)
   └─ put_item({… model_type, runtime:'onnx', detection:{…}})
        │  (SageMaker runs train.py: source-ref download → YOLO dataset → fine-tune → model.onnx)
        ▼
GET /training/{id}  → status sync + metrics from FinalMetricDataList
        │
        ▼
POST /training/{id}/compile   → Compilation_Bypass (compilation_skipped)
POST /training/{id}/package   → package_trained_detection_component() → ZIP → packaged_components
POST /training/{id}/publish   → (unchanged) Greengrass model component per target
```

## Architecture

### Shared module: `layers/shared/python/detection_training.py` (new)

Pure, boto3-free helpers used by `training.py`, `compilation.py`, `packaging.py` and tests. Keeping the predicate and the manifest builder in one shared module means the three Lambdas can never disagree about what a Detection_Training_Job is or what its Device_Manifest looks like.

```python
MODEL_TYPE_OBJECT_DETECTION = 'object_detection'
DETECTION_STAGE_TYPE = 'yolo_object_detection'
DETECTION_DEFAULTS = dict(imgsz=1280, epochs=100, batch=4, base_weights='yolo11s.pt',
                          patience=30, score_threshold=0.25, iou_threshold=0.45, onnx_opset=17)
DETECTION_METRIC_DEFINITIONS = [
    {'Name': 'test:mAP50',    'Regex': r'"test_map50": ([0-9.eE+-]+)'},
    {'Name': 'test:mAP50-95', 'Regex': r'"test_map50_95": ([0-9.eE+-]+)'},
    {'Name': 'test:precision','Regex': r'"test_precision": ([0-9.eE+-]+)'},
    {'Name': 'test:recall',   'Regex': r'"test_recall": ([0-9.eE+-]+)'},
]

def is_trained_detection_record(job: dict) -> bool
    # model_type == 'object_detection' AND runtime == 'onnx' AND source in (None, 'trained').
    # `runtime` is the discriminator: onnx-jetson-publish-packaging's fixtures seed
    # object_detection + source='trained' records whose artifact is a TorchScript .pt
    # and which still go through Neo / the 'onnx' export job. Only training.py's
    # detection branch writes runtime='onnx'.

def resolve_detection_training_image(region: str, override: str | None) -> str
    # override if non-empty else f"763104351884.dkr.ecr.{region}.amazonaws.com/pytorch-training:2.5.1-gpu-py311-cu124-ubuntu22.04-sagemaker"

def parse_detection_hyperparameters(raw: dict) -> dict
    # applies DETECTION_DEFAULTS, coerces types, raises ValueError naming the field on any violation (Req 3.8)

def detect_bbox_attribute(entry: dict) -> str | None
    # 'bounding-box' if present; else the first key K where entry[f'{K}-metadata'].type == 'groundtruth/object-detection'

def validate_detection_manifest_entry(entry: dict) -> dict
    # returns {'valid': bool, 'errors': [...], 'class_names': [...], 'attribute': K, 'detected_attributes': [...]}

def build_detection_device_manifest(*, image_width, image_height, num_classes, class_names,
                                    score_threshold, iou_threshold, preserve_aspect=True) -> dict
    # exact shape of generate_dda_package's ONNX/yolo detection manifest + a `dataset` block (Req 5.3, 5.7)

def build_sourcedir_tarball(code_dir: str, out_path: str) -> list[str]
    # tars every regular file in code_dir flat at the archive root; raises FileNotFoundError if train.py missing
```

`build_detection_device_manifest` deliberately duplicates the ~40 lines of `model_converter.generate_dda_package`'s detection branch rather than importing it: `model_converter.py` is a Lambda handler with module-level boto3 clients and is preservation-tracked, so importing it from the shared layer would be both heavy and hash-sensitive. A parity test (Req 5.7) pins the two together instead.

### `training.py`

`create_training_job` gains one early branch after `valid_model_types` and the access check:

```python
if model_type == MODEL_TYPE_OBJECT_DETECTION:
    return _create_detection_training_job(user, body, usecase, model_name, model_version,
                                          dataset_manifest_s3, instance_type, max_runtime, ...)
```

Everything below that branch is untouched, so the LFV path is byte-identical. `_create_detection_training_job`:

1. `parse_detection_hyperparameters(body.get('hyperparameters', {}))` → 400 on `ValueError`.
2. `validate_detection_manifest(dataset_manifest_s3, usecase)` (module-level, uses `get_s3_client_for_bucket` like the marketplace validator, reads the first 10 KB, parses line 1, delegates to `validate_detection_manifest_entry`) → 400 with `error`, `details`, `detected_attributes` on failure. `class_names = body.get('class_names') or result['class_names']`.
3. Job naming identical to LFV (`safe_model_name-timestamp`, 63-char truncation).
4. `code_dir = os.environ.get('DETECTION_TRAINING_CODE_DIR') or os.path.join(os.path.dirname(__file__), 'detection_training')`; `build_sourcedir_tarball` into a `TemporaryDirectory`; upload with the use-case S3 client (`get_usecase_client('s3', …)`) to `models/detection-training/{training_job_name}/sourcedir.tar.gz`.
5. `image = resolve_detection_training_image(usecase_region, os.environ.get('DETECTION_TRAINING_IMAGE'))`.
6. `env = {'MANIFEST_S3': manifest, 'IMGSZ': str(imgsz), 'EPOCHS': …, 'BATCH': …, 'BASE_WEIGHTS': …, 'PATIENCE': …, 'ONNX_OPSET': '17'}`; `HyperParameters = {'sagemaker_program': 'train.py', 'sagemaker_submit_directory': code_s3, **env}`; `Environment = env`.
7. `create_training_job(TrainingJobName, RoleArn=<same DDASageMakerExecutionRole>, AlgorithmSpecification={'TrainingImage': image, 'TrainingInputMode': 'File', 'MetricDefinitions': DETECTION_METRIC_DEFINITIONS}, HyperParameters, Environment, InputDataConfig=[], OutputDataConfig={'S3OutputPath': path_builder.get_training_output_uri(job)}, ResourceConfig={'InstanceType': instance_type, 'InstanceCount': 1, 'VolumeSizeInGB': 60}, StoppingCondition={'MaxRuntimeInSeconds': max_runtime}, EnableNetworkIsolation=False, Tags=<same four>)`.
8. `put_item` with the LFV base fields plus `runtime: 'onnx'`, `algorithm_uri: image`, `detection: {detection_arch: 'yolo', network_input_width: imgsz, network_input_height: imgsz, class_names, num_classes, score_threshold, iou_threshold, preserve_aspect: True, imgsz, epochs, batch, base_weights, patience, onnx_opset: 17, sourcedir_s3: code_s3}`. Floats are stored as `Decimal(str(x))` (DynamoDB rejects Python floats).
9. Same audit event and 201 body. `ClientError` handling is shared with LFV via the enclosing `try`.

Defaults when the request omits them: `instance_type = 'ml.g4dn.xlarge'`, `max_runtime_seconds = 10800`. The `default_max_runtime` ladder in the LFV code is left alone; the detection branch reads `body.get('max_runtime_seconds', 10800)` itself.

`get_training_job` gains, inside the existing `if job.get('training_job_name')` block after `describe_training_job`: if `is_trained_detection_record(job)` and `sm_response['TrainingJobStatus'] == 'Completed'` and not `job.get('metrics')`, build `metrics = {m['MetricName']: Decimal(str(m['Value'])) for m in sm_response.get('FinalMetricDataList', [])}` and, if non-empty, `update_item(SET metrics = :m)` and set `job['metrics']`. This runs independently of the status-changed block so it also fires when `training_events.py` already recorded Completed. `create_response` already serializes Decimals.

### Detection_Entry_Point (`datasets/detection_training/train.py`)

`stage_data()` changes so `IMAGES_S3` is optional:

```python
if IMAGES_S3:
    <existing prefix listing>
else:
    for each manifest line: entry['source-ref'] → download to images/<basename>
    warn on basename collisions (the converter keys files by basename)
```

`main()` requires only `MANIFEST_S3`. Nothing else in the entry point changes; the README launch snippet still works because `IMAGES_S3` remains honoured. The README gets a short "portal integration" update (the §"Portal integration" section is now closed).

### `compilation.py`

`start_compilation_job`'s bypass condition becomes `if _is_onnx_import(training_job) or is_trained_detection_record(training_job):`. The log line distinguishes the two; the response body is identical. No other change — `extract_and_repackage_model` is never reached for a detection record (it would raise on the missing `mochi.json`).

### `packaging.py`

New pure-ish function `package_trained_detection_component(trained_model_s3, training_job, s3_client, usecase) -> str`:

1. Download/extract the Detection_Artifact; find the `.onnx` by recursive scan (`FileNotFoundError` if none).
2. Read `training_metadata.json` if present → `imgsz`, `metrics`. `network = meta.get('imgsz') or record.detection.network_input_width`; log a warning if both exist and differ (Req 5.5).
3. `manifest = build_detection_device_manifest(image_width=network, image_height=network, num_classes=…, class_names=…, score_threshold=…, iou_threshold=…, preserve_aspect=record.detection.get('preserve_aspect', True))`.
4. Payload: `manifest.json` at root, `yolo_object_detection/model.onnx`; zip; upload to `model_artifacts/model-{uuid}/…zip` in the use-case bucket (same key scheme as `package_onnx_component`).

`package_components` gains a branch directly after the vLLM bypass and before the ONNX-import bypass:

```python
if is_trained_detection_record(training_job):
    <same shape as the ONNX-import bypass, but calls package_trained_detection_component and audits runtime='onnx', source='trained-detection'>
```

It is a separate block (not a merge with the import branch) so the imported-ONNX code stays textually identical and the preservation-style packaging tests keep their exact expectations. `packaging.py` is tracked by the IAM out-of-scope guard, so its sha256 is rebaselined in `iam_out_of_scope_baseline.json` with a note.

### Frontend

**`src/utils/manifestFormat.ts` (new)** — `classifyManifestFormat(sampleEntry): 'detection' | 'ground-truth' | 'dda' | 'unknown'`, extracted from `checkManifestFormat` so it is unit-testable:

```ts
if ('bounding-box' in entry) return 'detection';
if (Object.entries(entry).some(([k, v]) => k.endsWith('-metadata') && String(v?.type ?? '').includes('object-detection'))) return 'detection';
if (Object.keys(entry).some(k => k.endsWith('-metadata') && k !== 'anomaly-label-metadata' && k !== 'anomaly-mask-ref-metadata')) return 'ground-truth';
if (entry['anomaly-label'] !== undefined) return 'dda';
return 'unknown';
```

**`CreateTraining.tsx`**
- `manifestFormat` state type gains `'detection'`; `checkManifestFormat` calls the classifier.
- `modelTypeOptions` gains `{ label: 'Object Detection (YOLO)', value: 'object_detection', description: 'Bounding-box detection. Fine-tunes a YOLO detector on a bounding-box manifest and exports ONNX for the DDA edge runtime. No Neo compilation step.' }`.
- `const isDetection = modelType.value === 'object_detection'`.
- Instance options: `detectionInstanceTypeOptions` (g4dn.xlarge default, g4dn.2xlarge, g5.xlarge, g5.2xlarge); the Select uses `isDetection ? detectionInstanceTypeOptions : instanceTypeOptions`; the model-type `onChange` sets `maxRuntime` to `'10800'` and `instanceType` to g4dn.xlarge when switching to detection, and restores g4dn.2xlarge when switching away.
- New state `detectionParams = { imgsz: '1280', epochs: '100', batch: '4', baseWeights: 'yolo11s.pt', patience: '30', scoreThreshold: '0.25', iouThreshold: '0.45' }` rendered inside a "Detection Settings" `Container` shown only when `isDetection` (image size and base weights as Selects, the rest as numeric Inputs).
- Submit payload: `hyperparameters` = detection params (numbers) when `isDetection`, else the existing seghead spread.
- `getValidationErrors()` gains the two mismatch rules (Req 6.4, 6.5); the Ground Truth transform rule is unchanged.
- Alerts: the GT-detected alert is unchanged (only fires for `'ground-truth'`); a new success alert for `'detection'`; the top info alert, "Manifest Requirements" alert, "Max Runtime" description and "Next Steps" content branch on `isDetection`.

**`CompilationTab.tsx`** — `isOnnxModel` gains `|| String(tj?.runtime).toLowerCase() === 'onnx'` (runtime alone, mirroring the backend predicate; an `object_detection` record without it is a TorchScript detector that still compiles through Neo).

**`TrainingDetail.tsx`** — `const isDetection = job?.model_type === 'object_detection'`; adds a "Model Type" KeyValue row; the headline metric card reads `isDetection ? metrics['test:mAP50'] : metrics['validation:accuracy']` with the matching label.

**`types/index.ts`** — `TrainingJob` gains `model_type?: string; source?: string; runtime?: string; detection?: Record<string, unknown>;`.

### Infrastructure (`compute-stack.ts`)

```ts
const detectionTrainingImage = String(this.node.tryGetContext('detectionTrainingImage') ?? '');
const detectionTrainingFiles = [
  path.join(repoRoot, 'datasets/detection_training/train.py'),
  path.join(repoRoot, 'datasets/detection_training/requirements.txt'),
  path.join(repoRoot, 'datasets/manifest_to_detector_dataset.py'),
  path.join(repoRoot, 'datasets/dedupe_frames.py'),
];
const trainingHandler = new lambda.Function(this, 'TrainingHandler', {
  …,
  code: lambda.Code.fromAsset(functionsDir, {
    bundling: {
      image: cdk.DockerImage.fromRegistry('public.ecr.aws/amazonlinux/amazonlinux:2023'),
      volumes: [{ hostPath: path.join(repoRoot, 'datasets'), containerPath: '/datasets' }],
      command: ['bash', '-c', 'cp -r /asset-input/. /asset-output/ && mkdir -p /asset-output/detection_training && cp /datasets/detection_training/train.py /datasets/detection_training/requirements.txt /datasets/manifest_to_detector_dataset.py /datasets/dedupe_frames.py /asset-output/detection_training/'],
      local: { tryBundle(outputDir) { fs.cpSync(functionsDir, outputDir, { recursive: true }); fs.mkdirSync(…detection_training); for (f of detectionTrainingFiles) fs.copyFileSync(f, …); return true; } },
    },
  }),
  environment: { ...lambdaEnvironment, CODE_VERSION: '2026-09-13-v1', DETECTION_TRAINING_IMAGE: detectionTrainingImage },
  memorySize: 512,
  timeout: cdk.Duration.seconds(120),
});
```

No IAM change. The `sagemaker:CreateTrainingJob` grant is unscoped on `training-job/*`, `iam:PassRole` already covers `DDASageMakerExecutionRole`, and the S3 data-plane grant covers the use-case bucket upload. The use-case account's `DDASageMakerExecutionRole` already has ECR pull permissions (needed for the DLC image) and S3 access to the use-case/data buckets (needed for the manifest, images, sourcedir and output).

## Components and Interfaces

### Request contract (`POST /training`, detection)

```json
{
  "usecase_id": "...", "model_name": "blue-plate", "model_version": "2.0.0",
  "model_type": "object_detection",
  "dataset_manifest_s3": "s3://bucket/labeled/labeling-xxx/output.manifest",
  "instance_type": "ml.g4dn.xlarge",
  "max_runtime_seconds": 10800,
  "hyperparameters": { "imgsz": 1280, "epochs": 100, "batch": 4, "base_weights": "yolo11s.pt",
                       "patience": 30, "score_threshold": 0.25, "iou_threshold": 0.45 },
  "class_names": ["blue_plate"]            // optional override of the manifest class-map
}
```

### SageMaker job contract

| Field | Value |
|---|---|
| `AlgorithmSpecification.TrainingImage` | `DETECTION_TRAINING_IMAGE` or regional DLC default |
| `AlgorithmSpecification.TrainingInputMode` | `File` |
| `AlgorithmSpecification.MetricDefinitions` | the four `test:*` regexes |
| `HyperParameters` | `sagemaker_program`, `sagemaker_submit_directory`, `MANIFEST_S3`, `IMGSZ`, `EPOCHS`, `BATCH`, `BASE_WEIGHTS`, `PATIENCE`, `ONNX_OPSET` |
| `Environment` | same seven env names |
| `InputDataConfig` | `[]` |
| `EnableNetworkIsolation` | `False` |
| `ResourceConfig` | `{InstanceType, InstanceCount: 1, VolumeSizeInGB: 60}` |
| `StoppingCondition` | `{MaxRuntimeInSeconds: max_runtime}` |

### Detection_Record_Fields (DynamoDB)

```
runtime: 'onnx'
algorithm_uri: <TrainingImage>
detection: {
  detection_arch: 'yolo', network_input_width: N, network_input_height: N,
  class_names: [...], num_classes: k,
  score_threshold: Decimal, iou_threshold: Decimal, preserve_aspect: True,
  imgsz: N, epochs, batch, base_weights, patience, onnx_opset: 17,
  sourcedir_s3: 's3://.../sourcedir.tar.gz'
}
metrics: {'test:mAP50': Decimal, ...}          # filled by GET /training/{id} once Completed
```

### Device_Manifest for a trained detector

```json
{
  "runtime": "onnx", "runtime_artifact": "model.onnx", "task": "object_detection",
  "model_graph": {"model_graph_type": "single_stage_model_graph",
    "stages": [{"type": "yolo_object_detection", "input_shape": [1,3,N,N], "output_shape": [1,k+4,8400],
                "image_width": N, "image_height": N, "image_range_scale": true, "normalize": false,
                "threshold": 0.25, "num_classes": k}]},
  "input_shape": [1,3,N,N],
  "preprocessing": {"resize": [N,N], "channel_order": "RGB"},
  "dataset": {"image_width": N, "image_height": N},
  "detection": {"layout": "yolo", "num_classes": k, "score_threshold": 0.25, "network_input": N,
                "preserve_aspect": true, "iou_threshold": 0.45, "class_names": [...]}
}
```

`output_shape`'s third dimension is the nominal 8400 that `generate_dda_package` writes; the device decoder reads the real shape from the graph, so the value is informational (the real exported shape for 1280 is 33600).

## Data Models

- `TrainingJobs` item: additive fields only (`runtime`, `detection`, `metrics`); no schema/GSI change.
- No new tables, buckets or S3 prefixes beyond `models/detection-training/{job}/sourcedir.tar.gz` in the use-case bucket (sibling of the existing `models/onnx-export/{job}/`).

## Error Handling

| Condition | Response |
|---|---|
| Bad hyperparameter | 400 `{error: "Invalid hyperparameter 'imgsz': must be a multiple of 32 between 320 and 2048"}` |
| Classification manifest for detection | 400 `{error: "Object Detection requires a bounding-box manifest", details: [...], detected_attributes: [...]}` |
| Manifest missing | 400 `{error: "Manifest file not found: s3://..."}` |
| Bundled entry point missing (`train.py`) | 500 via generic handler; logged with the resolved `code_dir` |
| SageMaker `ResourceLimitExceeded` (g4dn.xlarge quota is 1) | 429 (existing mapping) |
| Artifact without `.onnx` at package time | 500 `{error: "Failed to package detection component: No .onnx model file found in trained detection artifact"}` |

Records are written only after `create_training_job` succeeds, exactly as today, so a rejected launch leaves no orphan row.

## Testing Strategy

Backend (`edge-cv-portal/backend`, `python3 -m pytest tests/<file> -q -p no:cacheprovider`, moto `aws_stack` fixture, `_load_module` + `FakeSageMakerService` pattern from `test_onnx_compile_diagnostics_units.py`):

- `tests/test_detection_training_shared.py` — pure helpers: hyperparameter parsing (defaults, each violation), `detect_bbox_attribute` / `validate_detection_manifest_entry` on a DDA `bounding-box` entry, a GT job-named entry, a classification entry; `resolve_detection_training_image`; `build_sourcedir_tarball` (flat root, four files); Hypothesis property that any valid hyperparameter dict round-trips.
- `tests/test_detection_training_create.py` — `create_training_job`: LFV classification request produces the exact pre-change SageMaker kwargs and item (preservation); detection request produces `TrainingImage`, script-mode hyperparameters, `Environment`, `InputDataConfig=[]`, `EnableNetworkIsolation=False`, `MetricDefinitions`, sourcedir object in the use-case bucket, and the record with `runtime`/`detection`; detection with a classification manifest → 400; bad hyperparameter → 400 and no SageMaker call; `get_training_job` hydrates `metrics` from `FinalMetricDataList`.
- `tests/test_detection_training_compile_package.py` — compile bypass for a detection record (no SageMaker calls, `compilation_skipped`); package builds the ZIP with `manifest.json` + `yolo_object_detection/model.onnx` and the Req 5.3 content; artifact without `.onnx` → 500 and `packaged_components` absent; imported-ONNX and LFV records untouched.
- `tests/test_detection_manifest_parity.py` — `build_detection_device_manifest` equals `generate_dda_package`'s `export_artifacts/manifest.json` (minus `dataset`) for a few parameter sets.

Frontend (`edge-cv-portal/frontend`, `npx vitest run <file>`):
- `src/utils/manifestFormat.test.ts` — classifier cases for DDA bounding-box, GT object-detection, GT classification (→ ground-truth), DDA classification, empty.
- `src/components/CompilationTab.detection.test.tsx` — a completed `object_detection` job with no compilation jobs renders Component Actions, not "Start Compilation".
- `npm run build` (`tsc && vite build`).

Gates:
- `python3 -m pytest test/backend-test/security/preservation -q -p no:cacheprovider --noconftest --ignore=…deserialization_roundtrip.py` after rebaselining `packaging.py`.
- User action: portal deploy (never concurrent with a component build; `cdk.out` moved aside first), then on-device verification of a retrained detector component on the JP7 DLAP.
