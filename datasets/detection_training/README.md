# Detector training on SageMaker — script mode (YOLO and RF-DETR)

Two SageMaker script-mode entry points fine-tune a detector on a DDA / Ground
Truth ObjectDetection manifest and export ONNX for the DDA edge runtime:

| entry point | arch | trainer | device stage / postprocessor |
|---|---|---|---|
| `train.py` | `yolo` | ultralytics YOLO | `yolo_object_detection` / `YoloDetectionPostProcessor` |
| `train_rfdetr.py` | `rf_detr` | rfdetr 1.10.1 (nano / small / medium / large) | `rf_detr_object_detection` / `RfDetrDetectionPostProcessor` |

Both run entirely inside the training container: download the manifest and
images, build a dataset with `../manifest_to_detector_dataset.py`, fine-tune,
export ONNX, and write `model.onnx`, the fine-tunable checkpoint and
`training_metadata.json` to `/opt/ml/model`. Everything trainer-agnostic
(S3 staging, converter invocation, `hp()` env parsing, base-weights download,
metadata) lives in the sibling `_common.py`.

YOLO closed the "§3 gap" from `docs/detection-training-gap.md`
(`.kiro/specs/portal-detection-training/`); RF-DETR and base-model
fine-tuning were added by `.kiro/specs/rfdetr-training-and-transfer-learning/`.
Both are wired into the portal — see [Portal integration](#portal-integration).

## Bundle layout matters

Run `./build_sourcedir.sh --arch yolo|rf_detr [output.tar.gz]` to produce the
tarball (default arch `yolo`; default output `sourcedir.tar.gz` /
`sourcedir-rfdetr.tar.gz` beside the script). Do not tar the repo layout
directly: each entry point resolves `_common.py` and the converter as
siblings of itself, the converter imports `dedupe_frames` from its own
directory, and SageMaker extracts the archive flat, so every file must be at
the archive root.

One arch per tarball. The bundle is exactly the chosen entry point, **its**
requirements file renamed to `requirements.txt` (the SageMaker toolkit
pip-installs precisely that name), `_common.py`, and the two converter files:

```
--arch yolo                          --arch rf_detr
  train.py                             train_rfdetr.py
  requirements.txt                     requirements.txt   (<- requirements-rfdetr.txt)
  _common.py                           _common.py
  manifest_to_detector_dataset.py      manifest_to_detector_dataset.py
  dedupe_frames.py                     dedupe_frames.py
```

The portal's `detection_training.build_sourcedir_tarball` produces the same
member set. An unknown `--arch` exits 2 with a usage line and writes nothing.

### Pins

`requirements.txt` (YOLO): `ultralytics==8.3.40`, `onnx==1.22.0`,
`onnxruntime==1.19.2`, `onnxslim==0.1.34`, `numpy<2`.

onnx 1.22 stamps IR version 13 on graphs it re-serialises (ultralytics'
onnxslim pass does), and onnxruntime older than 1.20 cannot load that. Both
entry points therefore lower the exported model to IR 10 with
`_common.cap_onnx_ir_version`, the IR every earlier export carried. The
opset-17 graph itself is unchanged.

`requirements-rfdetr.txt` (RF-DETR): `rfdetr[train,onnx]==1.10.1`,
`onnx==1.22.0`, `onnxruntime==1.19.2`, `numpy<2`. The `train` extra brings
pytorch_lightning / torchmetrics / pycocotools for `.train()` and
`.evaluate()`; `onnx` brings onnxsim / onnx_graphsurgeon for `.export()`.
There is no `onnxexport` extra in 1.10.1. Both launch examples below use the
same `pytorch-training:2.5.1-gpu-py311-cu124` DLC.

## Environment contract

Both entry points read their hyperparameters from the job `Environment`
(SageMaker exports them as env vars; `_common.hp()` parses them).

Common to both:

| variable | required | meaning |
|---|---|---|
| `MANIFEST_S3` | yes | `s3://` URI of the ObjectDetection output manifest |
| `IMAGES_S3` | no | image prefix to download wholesale; when unset (the portal path) exactly the manifest's `source-ref` URIs are downloaded |
| `BASE_WEIGHTS_S3` | no | `s3://` URI of a checkpoint to fine-tune from instead of the published weights (see [Base weights](#base-weights-transfer-learning)) |
| `BASE_WEIGHTS_MEMBER` | no | member to extract when `BASE_WEIGHTS_S3` is a tarball; defaults to the arch's own checkpoint name |

YOLO (`train.py`):

| variable | default | meaning |
|---|---|---|
| `IMGSZ` | `1280` | square letterbox size for training and export |
| `EPOCHS` | `100` | |
| `BATCH` | `4` | |
| `BASE_WEIGHTS` | `yolo11s.pt` | published ultralytics checkpoint (ignored when `BASE_WEIGHTS_S3` is set) |
| `PATIENCE` | `30` | early-stopping patience |
| `ONNX_OPSET` | `17` | |

RF-DETR (`train_rfdetr.py`):

| variable | default | meaning |
|---|---|---|
| `RFDETR_SIZE` | `small` | `nano` (384) / `small` (512) / `medium` (576) / `large` (704) — native resolution in parentheses |
| `RESOLUTION` | size's native | square input; **must be a multiple of 32 in [224, 1120]**, checked before any download |
| `EPOCHS` | `100` | |
| `BATCH` | `4` | per-step batch (T4 configuration) |
| `GRAD_ACCUM` | `4` | gradient-accumulation steps (effective batch = `BATCH × GRAD_ACCUM`) |
| `LR` | `1e-4` | |
| `PATIENCE` | `10` | early-stopping patience |
| `ONNX_OPSET` | `17` | |

Neither entry point takes an `InputDataConfig` channel: both need the whole
manifest to build the dataset, so they pull from S3 themselves via
`MANIFEST_S3` / `IMAGES_S3` rather than a per-record streaming channel.

### Base weights (transfer learning)

When `BASE_WEIGHTS_S3` is set, `_common.fetch_base_weights` downloads it into
the container and the run starts from that checkpoint instead of the published
weights. The object may be:

- a prior job's `model.tar.gz` (any `.tar.gz` / `.tgz` / `.tar`, or gzip-tar by
  content) — the checkpoint named by `BASE_WEIGHTS_MEMBER` is extracted from
  it; the default member is the arch's own artifact, `best.pt` for YOLO and
  `checkpoint_best_total.pth` for RF-DETR. A missing member is `FATAL` and the
  message lists the archive's contents;
- a bare weights file (`.pt` / `.pth`), used as-is.

The arch must match: a YOLO `best.pt` cannot seed an RF-DETR run or vice
versa. Both trainers re-initialise the class head when the manifest's class
set differs from the checkpoint's; the entry point logs both class lists so
that is never a surprise. There is deliberately no silent fallback to the
published weights — training from the wrong base is worse than failing.
`training_metadata.json` records `base_weights` (the S3 URI) and
`base_weights_member` when a base was used. This is how the portal's
**Base model** control works: it passes a completed job's `artifact_s3` as
`BASE_WEIGHTS_S3`.

## Launching

### YOLO

```python
import boto3, datetime, subprocess
BUCKET = 'ryvan-cookies'
job = 'blue-plate-yolo-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
prefix = f'models/detection/{job}'

subprocess.run(['./build_sourcedir.sh', '--arch', 'yolo', '/tmp/sourcedir.tar.gz'],
               check=True)
boto3.client('s3').upload_file(
    '/tmp/sourcedir.tar.gz', BUCKET, f'{prefix}/sourcedir.tar.gz')

boto3.client('sagemaker', region_name='us-east-1').create_training_job(
    TrainingJobName=job,
    AlgorithmSpecification={
        'TrainingImage': '763104351884.dkr.ecr.us-east-1.amazonaws.com/'
                         'pytorch-training:2.5.1-gpu-py311-cu124-ubuntu22.04-sagemaker',
        'TrainingInputMode': 'File',
    },
    RoleArn='arn:aws:iam::164152369890:role/DDASageMakerExecutionRole',
    HyperParameters={
        'sagemaker_program': 'train.py',
        'sagemaker_submit_directory': f's3://{BUCKET}/{prefix}/sourcedir.tar.gz',
    },
    Environment={
        'MANIFEST_S3': f's3://{BUCKET}/labeled/labeling-9cbcdb4c/output.manifest',
        'IMAGES_S3': f's3://{BUCKET}/imts-plates-luggage/',
        'IMGSZ': '1280', 'EPOCHS': '100', 'BATCH': '4',
        'BASE_WEIGHTS': 'yolo11s.pt', 'ONNX_OPSET': '17',
        # Optional: fine-tune from a prior job's artifact instead of yolo11s.pt
        # 'BASE_WEIGHTS_S3': f's3://{BUCKET}/models/detection/<prior-job>/output/model.tar.gz',
        # 'BASE_WEIGHTS_MEMBER': 'best.pt',
    },
    OutputDataConfig={'S3OutputPath': f's3://{BUCKET}/{prefix}/output/'},
    ResourceConfig={'InstanceType': 'ml.g4dn.xlarge', 'InstanceCount': 1,
                    'VolumeSizeInGB': 60},
    StoppingCondition={'MaxRuntimeInSeconds': 10800},
)
```

### RF-DETR

Same request, different bundle, entry point and environment:

```python
import boto3, datetime, subprocess
BUCKET = 'ryvan-cookies'
job = 'blue-plate-rfdetr-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
prefix = f'models/detection/{job}'

subprocess.run(['./build_sourcedir.sh', '--arch', 'rf_detr', '/tmp/sourcedir-rfdetr.tar.gz'],
               check=True)
boto3.client('s3').upload_file(
    '/tmp/sourcedir-rfdetr.tar.gz', BUCKET, f'{prefix}/sourcedir.tar.gz')

boto3.client('sagemaker', region_name='us-east-1').create_training_job(
    TrainingJobName=job,
    AlgorithmSpecification={
        'TrainingImage': '763104351884.dkr.ecr.us-east-1.amazonaws.com/'
                         'pytorch-training:2.5.1-gpu-py311-cu124-ubuntu22.04-sagemaker',
        'TrainingInputMode': 'File',
    },
    RoleArn='arn:aws:iam::164152369890:role/DDASageMakerExecutionRole',
    HyperParameters={
        'sagemaker_program': 'train_rfdetr.py',
        'sagemaker_submit_directory': f's3://{BUCKET}/{prefix}/sourcedir.tar.gz',
    },
    Environment={
        'MANIFEST_S3': f's3://{BUCKET}/labeled/labeling-9cbcdb4c/output.manifest',
        'IMAGES_S3': f's3://{BUCKET}/imts-plates-luggage/',
        'RFDETR_SIZE': 'small', 'RESOLUTION': '512',
        'EPOCHS': '100', 'BATCH': '4', 'GRAD_ACCUM': '4', 'LR': '1e-4',
        'PATIENCE': '10', 'ONNX_OPSET': '17',
        # Optional: fine-tune from a prior RF-DETR job's artifact
        # 'BASE_WEIGHTS_S3': f's3://{BUCKET}/models/detection/<prior-job>/output/model.tar.gz',
        # 'BASE_WEIGHTS_MEMBER': 'checkpoint_best_total.pth',
    },
    OutputDataConfig={'S3OutputPath': f's3://{BUCKET}/{prefix}/output/'},
    ResourceConfig={'InstanceType': 'ml.g4dn.xlarge', 'InstanceCount': 1,
                    'VolumeSizeInGB': 60},
    StoppingCondition={'MaxRuntimeInSeconds': 10800},
)
```

`BATCH=4, GRAD_ACCUM=4` is the documented T4 (`ml.g4dn.xlarge`) configuration;
`medium` / `large` are more comfortable on `ml.g5.xlarge`.

`IMAGES_S3` is optional. When it is unset — which is how the portal launches
these jobs — the entry point downloads exactly the images named by the
manifest's `source-ref` URIs instead of listing a prefix. Set it only to
override that with a whole prefix (the original manual-launch behaviour).

**One job at a time.** The account quota for `ml.g4dn.xlarge` training usage is
**1 instance**. A second concurrent submission fails immediately with
`ResourceLimitExceeded` — the sourcedir uploads fine and no job is created,
which reads as a silent no-op unless you check the API response.

## Geometry contract

The two archs preprocess in opposite ways, and the device must reproduce each
one exactly. Both entry points emit the correct settings under
`device_manifest_hints` in `training_metadata.json`, and the portal's packager
writes them into the model manifest; if you package by hand, copy them.

| | YOLO (`train.py`) | RF-DETR (`train_rfdetr.py`) |
|---|---|---|
| dataset | `--format yolo` | `--format coco --coco-layout rfdetr` (`train/valid/test/_annotations.coco.json`, images beside the JSON) |
| resize | **letterbox** to square `IMGSZ` (aspect kept, padded) | **square resize** to `RESOLUTION` (aspect destroyed, no padding) |
| pixel scaling | 0–1 only | 0–1 then ImageNet mean/std |
| `preserve_aspect` | **`true`** | **`false`** |
| `normalize` | absent / `false` | **`true`** |
| ONNX input | `[1, 3, IMGSZ, IMGSZ]` | `[1, 3, RESOLUTION, RESOLUTION]` (named `input`) |
| ONNX outputs | one: `[1, C+4, N]` (4 box coords + `C` class scores per anchor) | two: `dets [1, 300, 4]` (cxcywh, normalised) + `labels [1, 300, C+1]` (the trailing slot is rfdetr's never-positive background slot) |
| postprocessing | `YoloDetectionPostProcessor`: decode, `score_threshold`, **NMS at `iou_threshold`** | `RfDetrDetectionPostProcessor`: `score_threshold`, keep the **`top_k`** (300) query/class pairs — **no NMS, no `iou_threshold`** |
| stage type | `yolo_object_detection` | `rf_detr_object_detection` |
| fine-tunable checkpoint in the artifact | `best.pt` | `checkpoint_best_total.pth` |

YOLO: train letterboxed at a square `IMGSZ`, export at the same square
`IMGSZ`, and set `preserve_aspect: true` so the device letterboxes
identically. A mismatch silently degrades detection — the squash path costs
~1.35x mean confidence and up to 5.7x on high-resolution frames
(`docs/detection-training-gap.md` §7).

RF-DETR: rfdetr trains with `SquareResize` (aspect-destroying) plus ImageNet
normalisation, so the device must **not** letterbox and **must** normalise.
Serving an RF-DETR graph letterboxed reintroduces the same class of mismatch
in the other direction. The `labels` tensor has `C+1` slots; `class_names[i]`
is slot `i` for `i < C`, and the last slot is ignored. The entry point verifies
the exported graph is exactly one `[1,3,R,R]` input and these two outputs and
exits `FATAL` otherwise.

Note the YOLO contract differs from `docs/detection-training-gap.md` §2, which
recommends a rectangular `1088x1280` input to get near-zero letterbox padding
for the 2001x2352 production framing. The reference run below used square
1280. Both are correct as long as training and serving agree; the rectangular
option just wastes less of the input on padding.

## Reference run (YOLO)

`blue-plate-yolo-20260912-231818` (us-east-1), `Completed`, 1227s billable on
one `ml.g4dn.xlarge`. Built from the four-file YOLO bundle of the time
(verified by sha256 against
`s3://ryvan-cookies/models/detection/blue-plate-yolo-20260912-231818/sourcedir.tar.gz`);
today's bundle adds `_common.py`.

| metric | value |
|---|---|
| test mAP@50 | 0.995 |
| test mAP@50-95 | 0.919 |
| test precision | 0.9989 |
| test recall | 1.0 |

ONNX output shape `[1, 5, 33600]` — 4 box coords + 1 class score, decoded by
`YoloDetectionPostProcessor`. Trained on 145 images / 390 boxes, single class.
Metrics come from the converter's leakage-safe `test` split (whole similarity
groups assigned to one split), not a random split — but all frames are from one
capture session, so they say nothing about a new camera or new lighting.

Both entry points print one `TEST METRICS: {json}` line (`test_map50`,
`test_map50_95`, `test_precision`, `test_recall`) that the portal's
`DETECTION_METRIC_DEFINITIONS` regexes capture; RF-DETR computes it by
re-evaluating the reloaded `checkpoint_best_total.pth` on the `test` split.

Do **not** request TensorRT for the YOLO graph: `OnnxRunner.__select_providers`
excludes it deliberately, as it mis-executes YOLO's in-graph DFL / anchor-grid
ops and silently returns empty results. CUDA EP is numerically faithful.

## Portal integration

The portal launches these exact entry points when a training job is created
with `model_type: object_detection`
(`edge-cv-portal/backend/functions/training.py`); `detection_arch` (`yolo`,
the default, or `rf_detr`) selects the entry point. At deploy time the CDK
`TrainingHandler` asset bundles `train.py`, `train_rfdetr.py`, `_common.py`,
both requirements files and the two converter files into a
`detection_training/` directory; at job creation the Lambda tars the chosen
arch's set flat (`detection_training.build_sourcedir_tarball`) into
`s3://{usecase-bucket}/models/detection-training/{job}/sourcedir.tar.gz` and
passes `MANIFEST_S3` plus the arch's hyperparameters above as the job
environment — and `BASE_WEIGHTS_S3` + `BASE_WEIGHTS_MEMBER` when a **Base
model** was chosen. The finished `model.onnx` is packaged straight into a
Greengrass model component with the arch's geometry settings
(`preserve_aspect: true` for YOLO; `normalize: true`, `preserve_aspect: false`,
`top_k` for RF-DETR) — no SageMaker Neo step. See
`.kiro/specs/portal-detection-training/` and
`.kiro/specs/rfdetr-training-and-transfer-learning/` for the wiring and
`docs/detection-training-gap.md` for the original analysis.

Launching by hand (above) still works and remains useful for experiments, but
such a run writes no portal training record.
