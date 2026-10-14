# Detector checkpoint import: exploration spike

Spec: `.kiro/specs/detector-checkpoint-import` (Requirement 1, task 1). Run on
2026-09-25 in account 164152369890 / us-east-1, on branch
`spec/detector-checkpoint-import`.

## Outcome

Every fixture behaved as intended in real SageMaker training jobs with
`EnableNetworkIsolation=True`, on a prototype Export_Image with the code baked
in and a single `checkpoint` input channel. Converted YOLO and RF-DETR graphs
load and run on onnxruntime 1.16.3 (the Fleet_Floor_Runtime) and match their
source models. The export of the portal's own blue-plate checkpoints is
numerically identical to the trainers' `model.onnx` (YOLO: at most 3e-4 px;
RF-DETR: bit-identical outputs). A conversion job takes about 2 to 3.5 minutes
from `CreateTrainingJob` to completion, about 1 minute of which is billable.

One pre-existing defect surfaced (out of scope, see the last section): YOLO
`model.onnx` files written by the portal's trainer are ONNX IR version 10, which
onnxruntime 1.16.3 refuses to load. Portal-trained YOLO components therefore
cannot load on JP5 or on the CPU / x86 images.

## Decisions

| Item | Decision | Evidence |
|---|---|---|
| Base image | `public.ecr.aws/docker/library/python:3.11-slim-bookworm@sha256:4b4c524d…444b` (linux/amd64 manifest, Python 3.11.16, glibc 2.36) | The trixie base (glibc 2.41) cannot load the onnxruntime 1.16.3 wheel: its pybind `.so` requests an executable stack, and glibc ≥ 2.41 refuses that in `dlopen` ("cannot enable executable stack … Invalid argument"). Moving the base keeps the floor wheel unmodified. |
| torch | `2.5.1+cpu`, torchvision `0.20.1+cpu` (PyTorch CPU index) | Trainer parity (both trainers' DLC). |
| ultralytics | `8.4.162` | Loads every fixture, including the PPE checkpoint saved by 8.4.2 and the YOLO26 / YOLOv10 checkpoints. Trainer parity is proven by the ft-v3 oracle below, not by a shared pin (the trainers use 8.3.40). |
| YOLO export args | `format=onnx, imgsz=S, opset=17, dynamic=False, simplify=True, nms=None, batch=1, device=cpu` (never `half`; `quantize` unused) | `nms=None` gives the raw one-to-many `[1, 4+C, N]` for YOLO11, YOLO26 and YOLOv10. `nms=False` gives `[1, 300, 6]` for YOLO26 and YOLOv10 (checked in the image). |
| Accepted heads | `ultralytics.nn.modules.head.Detect` and `…head.v10Detect`, only under `ultralytics.nn.tasks.DetectionModel` with `task == 'detect'` | YOLO26 checkpoints carry `Detect`. `v10Detect` has a trained one-to-many branch: with identical square-letterboxed inputs, its one-to-many + NMS export returns the same detections as its own one-to-one export on `bus.jpg` and `zidane.jpg` (5/5 and 2/2 matched, IoU ≥ 0.955, confidence within 0.08). YOLO26 behaves the same (5/5, 3/3, IoU ≥ 0.91). Segmentation, pose, OBB, classification, World, YOLOE and RT-DETR stay rejected by model class, task and head. |
| YOLO parity reference | The `Exporter` instance's own `.model` (the fused export-mode module that was traced), called on the same tensors | Built with `Model.export`'s argument assembly (`{**overrides, imgsz, data: None, …, mode: 'export'}`) and `Exporter(overrides=args, _callbacks=yolo.callbacks)(model=yolo.model)`. |
| rfdetr | `1.10.1` (the trainers' pin) | Loads the portal's own `checkpoint_best_total.pth` (R2′) and the published nano `.pth` (R2). The export of R2′ is bit-identical to its trainer's `model.onnx`. |
| RF-DETR size inference | Build each Apache-2.0 size shell (`pretrain_weights=None`) and require a unique exact key-and-shape match of the checkpoint's state_dict. The checkpoint's `model_name` is tried first when present. | R2′: small matches with 0 missing / 0 unexpected / 0 shape mismatches (the only size tried, via `model_name`). R2: nano matches exactly; small, medium and large differ by 22, 44 and 44 missing keys plus 1 shape. With `RFDETR_SIZE=nano` forced on R2′ the job fails with "matches 0 … sizes". |
| RF-DETR rejections | `model_name` in `RFDETRXLarge`, `RFDETR2XLarge`, `RFDETRSegXLarge`, `RFDETRSeg2XLarge` (PML-licensed), any `RFDETRSeg*` / `RFDETRKeypoint*`, legacy `RFDETRBase` / `RFDETRLargeDeprecated`, `segmentation_head.*` keys | `rfdetr.detr._CHECKPOINT_PLUS_MODEL_NAME_CLASS_SYMBOLS` and the variant map. |
| RF-DETR resolution | Native only: nano 384, small 512, medium 576, large 704. Requirement 4.3 changes accordingly. | rfdetr 1.10.1 re-derives `positional_encoding_size = resolution // patch_size` whenever `resolution` is passed explicitly (`config.py` model validator), so a non-native export no longer loads the checkpoint's positional grid. Requesting 640 for R2′ fails with "RF-DETR exports at the checkpoint's own resolution (512 for small)". Pre-flight rejects checkpoints whose `args.resolution` is not their size's native one. |
| Strict load | After `RFDETR*(pretrain_weights=…, trust_checkpoint=True, num_classes=C)`, the loaded state_dict is compared key-for-key and shape-for-shape again | `load_pretrain_weights` uses `strict=False`. |
| ONNX IR | Every export in this image is IR 8, opset 17, default domain only. The job still clamps IR > 9 down to 8 before the floor check, as a guard. | ft-v3's trainer `model.onnx` is IR 10 (see below). |
| Op domains | Default domain only (`ai.onnx`); no other domain is needed | All ten graphs. |
| Parity check | Run the source model and the exported graph on the exporter's own onnxruntime (1.30.0) **and** on 1.16.3, each against the torch reference, on a seeded tensor and `ultralytics/assets/bus.jpg` preprocessed as the device does. | The table below. |
| YOLO tolerance | Element-wise over the whole `[1, 4+C, N]` tensor: box 0.1 px, score 1e-3 | Worst measured: box 0.0063 px (yolo26x, yolo11n), score 5.4e-6. The one-to-many graph has no data-dependent selection. |
| RF-DETR tolerance | Detection-level: every query/class pair with score ≥ 0.25 must match one-to-one (same class, nearest box) within box 1e-3 (normalised) and score 5e-3, with no extra pairs. In addition, ≥ 75 % of the 300 query slots must have a one-to-one partner (linear assignment) with the same top class within the same tolerances. | Element-wise comparison is wrong for RF-DETR. On the published nano with `bus.jpg`, onnxruntime 1.16.3 reorders or replaces 15 of 300 tail queries (all scoring < 0.09, worst slot box difference 0.71), while torch and onnxruntime 1.30.0 agree to 2e-5. Every detection ≥ 0.25 still matched to box 8.1e-6 and score 2.0e-5. The slot-agreement floor keeps the check meaningful on inputs with no confident detection. |
| Checkpoint_Size_Cap | **512 MiB** (not 1 GiB) | Measured in a scratch Lambda shaped like ModelConverter (3,008 MB, 8 GiB `/tmp`, the ModelConverter role). A 1 GiB checkpoint costs 26.8–27.4 s before any SageMaker or DynamoDB call: download 11.2–12.2 s (≈ 90 MB/s), sha256 2.9 s, probe 0.23 s and the sidecar upload 11.6–13.0 s. That leaves no margin under the 29 s API Gateway timeout. 512 MiB costs about 14 s. The largest real fixture (the 366 MB published RF-DETR nano, optimizer state included) costs 7.0 s. The probe itself takes about 0.2 s whatever the size, because it reads only the zip directory and `data.pkl`. |
| Image delivery | The CDK `DDASageMakerExecutionRole` (usecase-account-stack) already grants `ecr:GetAuthorizationToken`, `BatchCheckLayerAvailability`, `GetDownloadUrlForLayer` and `BatchGetImage` on `*`, so it needs no new grant. The portal repository policy names the trusted use-case accounts' `DDASageMakerExecutionRole` ARNs. | The ten jobs pulled the image from a same-account repository under the deployed role. **Verified single-account only**: both use cases in this deployment (`cookies`, `aliens`) live in 164152369890, and no second account is available. |
| Image size | 2.52 GB | The "Downloading" stage, which includes the image pull, took 31–36 s per job. |

## Per-fixture results

Jobs ran on image `dda-detector-export-spike@sha256:0814885e…dab` (build
`spike5`), `ml.m5.xlarge`, 30 GB, `MaxRuntimeInSeconds=1800`. Parity is the
worst difference over both runtimes and both inputs. YOLO boxes are in network
pixels; RF-DETR values are detection-level (see the tolerance row). "Export" is
the job's own measurement; "billable" and "wall" (`CreationTime` →
`TrainingEndTime`, provisioning included) come from `DescribeTrainingJob`.

| Fixture | S | C | Verdict | Output | IR / opset | Floor 1.16.3 | Parity box / score | Export s | Billable s | Wall s |
|---|---|---|---|---|---|---|---|---|---|---|
| (a) PPE `best.pt` (8.4.2, sha256 `a00b6fce…2119`) | 640 | 4 | pass | `[1, 8, 8400]` | 8 / 17 | loads, runs, finite | 0.0018 / 2.5e-6 | 5.2 | 63 | 153 |
| (b) ft-v3 `best.pt` (8.3.40) | 1280 | 1 | pass | `[1, 5, 33600]` | 8 / 17 | ✓ | 0.0011 / 8.0e-7 | 14.3 | 79 | 133 |
| (c) `yolov8n.pt` | 640 | 80 | pass | `[1, 84, 8400]` | 8 / 17 | ✓ | 0.0014 / 1.0e-6 | 5.1 | 64 | 134 |
| (c) `yolo11n.pt` | 640 | 80 | pass | `[1, 84, 8400]` | 8 / 17 | ✓ | 0.0063 / 5.4e-6 | 5.4 | 63 | 121 |
| (c) `yolo26n.pt` | 640 | 80 | pass (one-to-many) | `[1, 84, 8400]` | 8 / 17 | ✓ | 0.0059 / 2.9e-6 | 5.6 | 64 | 155 |
| (c) `yolov10n.pt` | 640 | 80 | FATAL in this run (head allowlist was `Detect` only); **pass** after the head decision (local, `--network none`) | `[1, 84, 8400]` | 8 / 17 | ✓ | 0.0033 / 5.6e-6 | — | 58 | 136 |
| (d) `yolo11n-seg.pt` | 640 | 80 | FATAL: `checkpoint task is 'segment'; only object detection ('detect') converts` | — | — | — | — | — | 59 | 170 |
| stress: `yolo11x.pt` | 1280 | 80 | pass | `[1, 84, 33600]` | 8 / 17 | ✓ | 0.0034 / 3.2e-6 | 66.9 | 134 | 211 |
| (e) R2′ portal RF-DETR small `checkpoint_best_total.pth` | 512 | 1 | pass | `[1, 300, 4]` + `[1, 300, 2]` | 8 / 17 | ✓ | 8.3e-7 / 2.0e-6 | 14.4 | 79 | 173 |
| (e) R2 published RF-DETR nano `.pth` | 384 | 90 | pass | `[1, 300, 4]` + `[1, 300, 91]` | 8 / 17 | ✓ (slot agreement 0.95) | 8.1e-6 / 2.0e-5 | 15.4 | 73 | 122 |

Also run locally with `--network none` (the same image, no job):
- `yolo11x.pt` and `yolo26x.pt` at 640 pass with worst parity 0.0063 px and 6.2e-7. These fixed the YOLO tolerance.
- Negative cases fail with their reasons:
  - PPE declared with 5 classes: `checkpoint has 4 classes; the import recorded 5`.
  - R2′ at 640: `RF-DETR exports at the checkpoint's own resolution (512 for small)`.
  - R2′ with `RFDETR_SIZE=nano`: `matches 0 Apache-2.0 RF-DETR sizes`.
- PPE at 1280 passes: YOLO exports at any valid S.

Job behaviour confirmed:
- `/opt/ml/output/failure` surfaces as `FailureReason`, wrapped by SageMaker: `AlgorithmError: FATAL: checkpoint detection head is ultralytics.nn.modules.head.v10Detect; accepted: …, exit code: 1`. The portal stores this string verbatim (Requirement 7.4).
- `model.tar.gz` contains exactly two regular-file members, `model.onnx` and `training_metadata.json`, with no directory entry.
- Nothing in the job used the network. The only URL-like log lines are ultralytics' printed hints (`docs.ultralytics.com`, `netron.app`), and `YOLO_OFFLINE` / `YOLO_AUTOINSTALL=False` / `HF_HUB_OFFLINE` held.
- Stage timings: Starting 49–111 s, Downloading 31–36 s, Training 16–80 s, Uploading 7–18 s.

## Final-image gate (task 9.4)

Run on 2026-09-25, after every code change, on the final Export_Image: build
`dci-final1`, `dda-detector-export-spike@sha256:babcfbe0a695cfadf05ac3c87feed3331c379da4eed6721178555c825e582498`.
The `export_checkpoint.py`, `train_rfdetr.py` and `_common.py` baked into it
are byte-identical to the working tree (sha256 checked inside the image).

Unlike the spike, every job went through the portal's own code path, in the
order the convert route runs it:
- `classify_checkpoint` → `assess_checkpoint` → `validate_conversion_request`, with the body the Smart Import page sends (its `conversionLocksFor` pre-fill; generic names for the nano, which stores none);
- the checkpoint staged as the convert route's sidecar (`converted-models/<name>-<hex8>/checkpoint.<ext>`);
- `conversion_job_name` → `build_conversion_job_request` → `CreateTrainingJob`.

Every Completed artifact then went through `build_conversion_record` and
`validate_conversion_artifact`, the validator the packaging finalize runs. The
segmentation checkpoint is rejected by the pre-flight, so it was submitted a
second way, with the pre-flight bypassed, to prove the job's own defence.

| Fixture | S | C | Pre-flight | Job | Portal validator | Output | IR / opset | Parity box / score | Billable s | Wall s |
|---|---|---|---|---|---|---|---|---|---|---|
| (a) PPE `best.pt` | 640 | 4 | convertible | Completed | accepted | `[1, 8, 8400]` | 8 / 17 | 0.0018 / 2.5e-6 | 69 | 111 |
| (b) ft-v3 `best.pt` | 1280 | 1 | convertible | Completed | accepted | `[1, 5, 33600]` | 8 / 17 | 0.0011 / 8.0e-7 | 69 | 113 |
| (c) `yolov8n.pt` | 640 | 80 | convertible | Completed | accepted | `[1, 84, 8400]` | 8 / 17 | 0.0014 / 1.0e-6 | 63 | 106 |
| (c) `yolo11n.pt` | 640 | 80 | convertible | Completed | accepted | `[1, 84, 8400]` | 8 / 17 | 0.0063 / 5.4e-6 | 68 | 116 |
| (c) `yolo26n.pt` | 640 | 80 | convertible | Completed (one-to-many) | accepted | `[1, 84, 8400]` | 8 / 17 | 0.0059 / 2.9e-6 | 64 | 109 |
| (c) `yolov10n.pt` | 640 | 80 | convertible | Completed (one-to-many) | accepted | `[1, 84, 8400]` | 8 / 17 | 0.0039 / 7.5e-6 | 64 | 106 |
| (d) `yolo11n-seg.pt` | 640 | 80 | rejected (segmentation model; `segment` task) | Failed: `AlgorithmError: FATAL: checkpoint task is 'segment'; only object detection ('detect') converts, exit code: 1` | — | — | — | — | 59 | 103 |
| stress: `yolo11x.pt` | 1280 | 80 | convertible | Completed | accepted | `[1, 84, 33600]` | 8 / 17 | 0.0034 / 3.2e-6 | 129 | 169 |
| (e) R2′ RF-DETR small | 512 | 1 | convertible (small) | Completed | accepted | `[1, 300, 4]` + `[1, 300, 2]` | 8 / 17 | 8.3e-7 / 2.0e-6 | 79 | 122 |
| (e) R2 RF-DETR nano | 384 | 90 | convertible (nano) | Completed | accepted | `[1, 300, 4]` + `[1, 300, 91]` | 8 / 17 | 8.1e-6 / 2.0e-5 | 74 | 121 |

All ten verdicts are the spike's expected ones, including YOLOv10, which now
passes through its one-to-many branch. All jobs ran with
`EnableNetworkIsolation=True`. Every accepted artifact reports exporter
`ultralytics 8.4.162` or `rfdetr 1.10.1`, loaded on the fleet floor
(onnxruntime 1.16.3), and passed parity on both 1.16.3 and 1.30.0. Parity
values match the spike run to within float noise. The one visible change,
YOLOv10's score maximum (5.6e-6 to 7.5e-6), is still two orders of magnitude
inside the 1e-3 tolerance.

## Trainer-parity oracles

- **YOLO, ft-v3** (`blue-plate-yolo-ft-v3-20260922-172408`, ultralytics 8.3.40): our export of its `best.pt` and the job's own `model.onnx` have the same 320 nodes and opset 17. Their outputs match within 3.1e-4 px on boxes and 4.5e-7 on scores on four inputs: the seeded tensor, `bus.jpg`, a blue-plate training image (`u724uckx-023592ff…jpg`, top score 0.9076 in both) and the PPE sample image. The on-device oracle (0.769 / 0.932 / 0.915) is checked in task 11(d).
- **RF-DETR small** (`blue-plate-rfdetr-small-20260922-145600`): the same 1,550 nodes and **identical** outputs (max |d| = 0) on the seeded tensor, `bus.jpg` and the blue-plate image.

## Side finding, now fixed: trainer YOLO ONNX is IR 10

### The bug

The trainer's ft-v3 `model.onnx` declares `ir_version` 10 (producer pytorch 2.5.1, opset 17). onnxruntime 1.16.3 refuses it at load time (`Load model … failed: … model.cc:149`: unsupported IR version, maximum 9). Our IR-8 export of the same checkpoint loads. The RF-DETR trainer's output is IR 8 and loads.

So every portal-trained YOLO component published for `jetson-xavier-jp5` or `x86_64-cpu` (both on onnxruntime 1.16.3) failed to load there. The only verified target had been JP7 (1.23.2), and JP6 (1.20.1) also loads IR 10.

### Root cause

This was reproduced in a container with the trainer's exact pins: ultralytics 8.3.40, onnx 1.17.0 (`IR_VERSION` 10), onnxslim 0.1.34, onnxruntime 1.19.2 and torch 2.5.1. torch writes IR 8. `simplify=True` then runs onnxslim, which re-serializes the model and stamps the installed onnx's IR 10:

| Export setting | IR version |
|---|---|
| `simplify=True` | 10 |
| `simplify=False` | 8 |

Nothing in the opset-17 graph needs more than IR 8.

### Fix

The fix has two layers, and both lower the header only when the content allows it. They leave a graph alone if its default-domain opset is 21 or higher, if it has model-local functions, or if it uses UINT4 / INT4 / FLOAT4 element types. FLOAT8 raises the target to IR 9.

- **Trainer:** `train.py` `export()` calls `_common.normalize_onnx_ir_version`. It uses the onnx library, validates the result with `onnx.checker`, and records `ir_version` and `ir_version_exported` in `training_metadata.json`.
  - Upstream commit 89d1655 (Dependabot) later moved the trainers to onnx 1.22.0, which stamps IR 13. It also added `_common.cap_onnx_ir_version`, a ceiling of IR 10 for onnxruntime 1.19.2. `train.py` now runs both: the fleet-floor lowering first (13 → 8), then the ceiling, which is a no-op once the graph is at 8 and still applies if the lowering ever declines a graph.
- **Packager:** `packaging.package_trained_detection_component` calls `onnx_fleet_ir.normalize_fleet_ir_version`, which is pure stdlib and changes only the `ir_version` varint. This fixes existing trained records when they are re-packaged. It is a no-op for IR 9 and below, which includes every Conversion_Record.

### Verification

- **Fixed trainer under the trainer's pins:** metadata reads `ir_version 8, ir_version_exported 10`. The fixed graph loads and runs on 1.16.3, and its output equals the unfixed IR-10 export on 1.19.2 exactly (max |d| = 0).
- **Re-run after rebasing onto 617950a (onnx 1.22.0):** the export comes out of onnxslim as IR 13 and is written as IR 8 (metadata `ir_version 8, ir_version_exported 13`). It loads and runs on 1.16.3, and its output again equals the old IR-10 export exactly. The packager lowers an IR-13 header to 8 too.
- **Packager fix on the real ft-v3 artifact:** exactly one byte changes and the result loads on 1.16.3. On onnxruntime 1.30.0 the outputs equal the original's exactly (max |d| = 0). On 1.16.3 the real images (`bus.jpg` and a blue-plate frame) also match exactly, and the seeded tensor is within 2.4e-4 px / 1.8e-7. The unfixed file still fails on 1.16.3.

### Not yet fixed

Components already published keep their IR-10 graph until they are re-packaged and re-published.

## Final pins

- `requirements.lock`: 73 exact pins generated by `build-and-push.sh --lock` from `requirements.in`. The notable ones are ultralytics 8.4.162, rfdetr[onnx] 1.10.1, onnx 1.23.0, onnxslim 0.1.96, onnxruntime 1.30.0, numpy 2.4.6, opencv-python 5.0.0.93, transformers 5.17.0 and scipy 1.17.1. torch and torchvision are installed from the PyTorch CPU index; `pip check` is clean.
- `ort-floor.lock`: onnxruntime 1.16.3 and numpy 1.26.4, in `/opt/ort-floor`.

## Cleanup

- Removed after the measurement: the scratch Lambda `dci-spike-size-cap`, its log group, the 1 GiB probe object and the sidecar-upload probe objects under `converted-models/dci-spike-sizecap-test/`.
- Kept for the final-image re-run (tasks 3.3 and 9.4), then deleted: the ECR repository `dda-detector-export-spike` and the S3 prefix `s3://ryvan-cookies/dci-spike/`.
