# Detector (YOLO) training on SageMaker — script mode

`train.py` is the SageMaker script-mode entry point that fine-tunes a YOLO
detector on a DDA / Ground Truth ObjectDetection manifest and exports ONNX for
the DDA edge runtime. It runs entirely inside the training container: downloads
the manifest and images, builds a YOLO dataset with
`../manifest_to_detector_dataset.py`, fine-tunes letterboxed, exports ONNX with
**no in-graph NMS** (the device's `YoloDetectionPostProcessor` does NMS), and
writes `model.onnx`, `best.pt`, `training_metadata.json` to `/opt/ml/model`.

This is the missing "§3 gap" from `docs/detection-training-gap.md`. It is not
yet wired into the portal — see [Portal integration](#portal-integration).

## Bundle layout matters

Run `./build_sourcedir.sh` to produce the tarball. Do not tar the repo layout
directly: `train.py` resolves the converter as a sibling of itself, and
SageMaker extracts the archive flat, so all four files must be at the archive
root.

## Launching

```python
import boto3, datetime, subprocess
BUCKET = 'ryvan-cookies'
job = 'blue-plate-yolo-' + datetime.datetime.now().strftime('%Y%m%d-%H%M%S')
prefix = f'models/detection/{job}'

subprocess.run(['./build_sourcedir.sh', '/tmp/sourcedir.tar.gz'], check=True)
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
    },
    OutputDataConfig={'S3OutputPath': f's3://{BUCKET}/{prefix}/output/'},
    ResourceConfig={'InstanceType': 'ml.g4dn.xlarge', 'InstanceCount': 1,
                    'VolumeSizeInGB': 60},
    StoppingCondition={'MaxRuntimeInSeconds': 10800},
)
```

`InputDataConfig` is intentionally empty: the entry point needs the whole
manifest to build `data.yaml`, so it pulls from S3 itself via `MANIFEST_S3` /
`IMAGES_S3` rather than taking a per-record streaming channel.

`IMAGES_S3` is optional. When it is unset — which is how the portal launches
this job — the entry point downloads exactly the images named by the
manifest's `source-ref` URIs instead of listing a prefix. Set it only to
override that with a whole prefix (the original manual-launch behaviour).

**One job at a time.** The account quota for `ml.g4dn.xlarge` training usage is
**1 instance**. A second concurrent submission fails immediately with
`ResourceLimitExceeded` — the sourcedir uploads fine and no job is created,
which reads as a silent no-op unless you check the API response.

## Geometry contract

Train letterboxed at a square `IMGSZ`, export at the same square `IMGSZ`, and
set **`preserve_aspect: true`** in the device model manifest so the device
letterboxes identically. `training_metadata.json` emits this under
`device_manifest_hints`. A mismatch silently degrades detection — the squash
path costs ~1.35x mean confidence and up to 5.7x on high-resolution frames.

Note this differs from `docs/detection-training-gap.md` §2, which recommends a
rectangular `1088x1280` input to get near-zero letterbox padding for the
2001x2352 production framing. The reference run below used square 1280. Both
are correct as long as training and serving agree; the rectangular option just
wastes less of the input on padding.

## Reference run

`blue-plate-yolo-20260912-231818` (us-east-1), `Completed`, 1227s billable on
one `ml.g4dn.xlarge`. Built from this exact bundle (verified by sha256 against
`s3://ryvan-cookies/models/detection/blue-plate-yolo-20260912-231818/sourcedir.tar.gz`).

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

Do **not** request TensorRT for this graph: `OnnxRunner.__select_providers`
excludes it deliberately, as it mis-executes YOLO's in-graph DFL / anchor-grid
ops and silently returns empty results. CUDA EP is numerically faithful.

## Portal integration

The portal launches this exact entry point when a training job is created with
`model_type: object_detection` (`edge-cv-portal/backend/functions/training.py`).
At deploy time the CDK `TrainingHandler` asset bundles these four files into a
`detection_training/` directory; at job creation the Lambda tars them flat into
`s3://{usecase-bucket}/models/detection-training/{job}/sourcedir.tar.gz` and
passes `MANIFEST_S3` plus the user's `IMGSZ` / `EPOCHS` / `BATCH` /
`BASE_WEIGHTS` / `PATIENCE` as the job environment. The finished `model.onnx`
is packaged straight into a Greengrass model component with
`preserve_aspect: true` — no SageMaker Neo step. See
`.kiro/specs/portal-detection-training/` for the wiring and
`docs/detection-training-gap.md` for the original analysis.

Launching by hand (above) still works and remains useful for experiments, but
such a run writes no portal training record.
