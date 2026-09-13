# Requirements Document

## Introduction

The `portal-detection-training` spec made **YOLO** a first-class portal training source: a bounding-box manifest goes in, a letterboxed ONNX detector comes out, and it packages and publishes without Neo. Two things it deliberately left as disabled "coming soon" entries in the Create Training **Model Source** dropdown:

1. **RF-DETR** — the device already decodes RF-DETR ONNX (`rf_detr_object_detection` stage, `RfDetrDetectionPostProcessor`) and Smart Import already imports it, but there is no *training* entry point. RF-DETR's DINOv2 backbone often out-performs YOLO on small industrial datasets (RF100-VL), and its two-tensor NMS-free output side-steps the TensorRT/DFL hazard the YOLO graph carries on Jetson.
2. **Imported Model (BYOM) as a training source** — users want to *add training data and transfer-learn from a model they already have*: a previous portal-trained detector, or a checkpoint they brought in through Smart Import. Today every training run starts from the published COCO checkpoint (`yolo11s.pt`) and forgets everything a prior run learned.

This spec closes both. It adds an RF-DETR script-mode entry point that mirrors `datasets/detection_training/train.py` (same manifest → dataset converter, same artifact contract, same packaging path), teaches the converter the COCO layout RF-DETR requires, and adds a **base model** selection to detection training so a run can start from a prior training job's checkpoint or an imported fine-tunable checkpoint. Because the fine-tunable checkpoint formats that arrive via Smart Import are not fully characterised (ultralytics `.pt` vs raw `state_dict` vs TorchScript vs RF-DETR `.pth`), the transfer-learning half is gated behind an **exploration spike** whose findings decide the final design.

Everything is cloud-side except the RF-DETR device manifest, which is already supported; a retrained model reaches the device as its own Greengrass model component. No LocalServer build is required.

## Glossary

- **Detection_Training_Job**: a `TrainingJobs` record with `model_type = 'object_detection'` and `runtime = 'onnx'` (portal-detection-training Req 4.4).
- **Detection_Arch**: the detector family — `'yolo'` (single-tensor, NMS) or `'rf_detr'` (two-tensor, NMS-free top-k). Persisted as `detection.detection_arch`; selects the device stage type (`yolo_object_detection` / `rf_detr_object_detection`) and preprocessing (`normalize` false / true).
- **Model_Source**: the Create Training dropdown value: `marketplace`, `yolo`, `rf_detr`, `byom`.
- **RF_DETR_Entry_Point**: `datasets/detection_training/train_rfdetr.py` plus `requirements-rfdetr.txt`, bundled with `manifest_to_detector_dataset.py` and `dedupe_frames.py` into a flat `sourcedir.tar.gz` — sibling of the YOLO `train.py`.
- **RF_DETR_Size**: one of the Apache-2.0 detection checkpoints: `nano` (384), `small` (512), `medium` (576), `large` (704); the number is the native square resolution. `xlarge`/`2xlarge` are PML-licensed and are excluded.
- **COCO_RFDETR_Layout**: `dataset/{train,valid,test}/_annotations.coco.json` with the images *beside* the JSON in the same folder (RF-DETR's loader joins `dataset_dir/<split>/<file_name>`). Our converter's current COCO output uses `val` and `<split>/images/` and so is not loadable as-is.
- **Detection_Artifact**: the SageMaker `model.tar.gz` a Detection_Training_Job writes: `model.onnx`, `training_metadata.json`, and the fine-tunable checkpoint (`best.pt` for YOLO, `checkpoint_best_total.pth` for RF-DETR).
- **Base_Model**: the weights a training run starts from. Either a **published checkpoint** (`yolo11s.pt`, `RFDETRSmall` COCO weights), a **prior Detection_Training_Job**'s checkpoint, or an **Imported fine-tunable checkpoint**.
- **Fine_Tunable_Checkpoint**: a weights file the matching trainer can load and continue training: an ultralytics `.pt` (full pickled model or `{'model': ...}` checkpoint) for YOLO; an RF-DETR `.pth` (`checkpoint_best_total.pth` / `checkpoint_best_ema.pth`) for RF-DETR. An ONNX graph or a TorchScript `.pt` is **not** fine-tunable.
- **Imported_Model**: a `TrainingJobs` record with `source = 'imported'` created by Smart Import / Model Import, whose `artifact_s3` is a DDA package tarball.
- **Base_Model_Descriptor**: the persisted description of the Base_Model on a Detection_Training_Job: `{kind: 'published'|'training_job'|'imported', ref: <checkpoint name | training_id>, weights_s3: <s3 uri or null>, detection_arch, class_names}`.
- **Exploration_Spike**: a time-boxed investigation (task 1 of this spec) that establishes, with evidence, which Fine_Tunable_Checkpoint formats reach the portal and how each trainer loads them; its findings are recorded in `docs/transfer-learning-spike.md` and gate the design of Requirement 7.

## Requirements

### Requirement 1: RF-DETR training entry point

**User Story:** As a Data Scientist, I want to fine-tune an RF-DETR detector from a bounding-box manifest inside a SageMaker job, so that I get a DETR-family ONNX the device already knows how to serve.

#### Acceptance Criteria

1. THE RF_DETR_Entry_Point SHALL run as a SageMaker script-mode job on the same PyTorch GPU DLC as the YOLO entry point, reading `MANIFEST_S3` (required), optional `IMAGES_S3`, `RFDETR_SIZE` (default `small`), `RESOLUTION` (default: the size's native resolution), `EPOCHS` (default 100), `BATCH` (default 4), `GRAD_ACCUM` (default 4), `LR` (default `1e-4`), `PATIENCE` (default 10), `ONNX_OPSET` (default 17), and `BASE_WEIGHTS_S3` (optional, Requirement 7) from the environment.
2. WHEN `IMAGES_S3` is unset, THE RF_DETR_Entry_Point SHALL download the images named by the manifest's `source-ref` URIs exactly as the YOLO entry point does (shared helper, not a copy).
3. THE RF_DETR_Entry_Point SHALL build the dataset with `manifest_to_detector_dataset.py --format coco --coco-layout rfdetr` (Requirement 2) so the result is a COCO_RFDETR_Layout, and SHALL fail fast with a `FATAL:` message naming the split if any of `train/`, `valid/` is missing or empty.
4. THE RF_DETR_Entry_Point SHALL instantiate the RF_DETR_Size class (`RFDETRNano|Small|Medium|Large`) with `pretrain_weights` set to the resolved Base_Model checkpoint when one is given, and otherwise the package's published COCO weights, and SHALL call `.train(dataset_dir, epochs, batch_size, grad_accum_steps, lr, output_dir, resolution, early_stopping=True, early_stopping_patience=PATIENCE, run_test=True)`.
5. IF `RESOLUTION` is not a positive multiple of 56, THEN THE RF_DETR_Entry_Point SHALL exit with `FATAL: RESOLUTION must be a multiple of 56` before downloading anything (the DINOv2 patch/window grid requires it).
6. WHEN training completes, THE RF_DETR_Entry_Point SHALL export ONNX from `checkpoint_best_total.pth` at `RESOLUTION`, static batch 1, `ONNX_OPSET`, and SHALL verify with onnxruntime that the graph has exactly two outputs whose shapes are `[1, Q, 4]` and `[1, Q, num_classes]` for the same `Q`; any other shape SHALL fail the job with a `FATAL:` message quoting the shapes.
7. THE RF_DETR_Entry_Point SHALL write `model.onnx`, `checkpoint_best_total.pth` and `training_metadata.json` to `/opt/ml/model`, where `training_metadata.json` carries `{"detection_arch": "rf_detr", "resolution", "rfdetr_size", "epochs", "opset", "onnx_output_shapes", "metrics", "class_names", "base_model": <Base_Model_Descriptor or null>, "device_manifest_hints": {"layout": "rf_detr", "network_input": RESOLUTION, "preserve_aspect": false, "normalize": true, "score_threshold": 0.5, "top_k": 300}}`.
8. THE RF_DETR_Entry_Point SHALL print one `TEST METRICS: {json}` line with `test_map50`, `test_map50_95`, `test_precision`, `test_recall` computed on the converter's leakage-safe `test` split, so the existing `DETECTION_METRIC_DEFINITIONS` regexes capture them unchanged.
9. THE RF_DETR_Entry_Point SHALL NOT letterbox: RF-DETR trains on a square resize (`SquareResize`, aspect-destroying) with ImageNet normalisation, and the device manifest SHALL therefore say `preserve_aspect: false`, `normalize: true`. Serving letterboxed would reintroduce the geometry mismatch the YOLO work fixed, in the other direction.
10. `datasets/detection_training/build_sourcedir.sh` SHALL accept `--arch yolo|rf_detr` (default `yolo`) and bundle the matching entry point + requirements with the two converter files, all flat; the README SHALL document both.

### Requirement 2: Converter emits the RF-DETR COCO layout

**User Story:** As the entry point, I want the dataset converter to write exactly what RF-DETR's loader reads, so that I don't post-process directories inside a GPU job.

#### Acceptance Criteria

1. `datasets/manifest_to_detector_dataset.py` SHALL accept `--coco-layout {nested,rfdetr}` (default `nested`, the current behaviour: `<split>/images/<file>` with split names `train|val|test`).
2. WHEN `--coco-layout rfdetr` is given, THE converter SHALL write `<split>/_annotations.coco.json` with the images placed **beside** it (`<split>/<file>`), SHALL name the validation split `valid`, and SHALL keep `category_id` 1-based with `categories` in `class_names` order.
3. THE converter's existing tests (`edge-cv-portal/backend/tests/test_manifest_to_detector_dataset.py`) SHALL pass unchanged, and new cases SHALL cover the `rfdetr` layout (file placement, split names, identical annotation content to `nested`).
4. THE leakage-safe grouping, negative stratification and split fractions SHALL be identical between layouts (the layout flag affects only paths and split names).

### Requirement 3: Portal launches RF-DETR training

**User Story:** As a Data Scientist, I want to pick "RF-DETR - Object Detection" as the Model Source and start a job, so that RF-DETR training is as one-click as YOLO.

#### Acceptance Criteria

1. THE `MODEL_SOURCE_OPTIONS` entry for `rf_detr` SHALL be enabled, with Model Type filtered to Object Detection.
2. WHEN `POST /training` is called with `model_type = 'object_detection'` and `detection_arch = 'rf_detr'` (a new top-level request field, default `'yolo'` so existing callers are unchanged), THE Portal SHALL launch the RF_DETR_Entry_Point: `sagemaker_program = 'train_rfdetr.py'`, hyperparameters from Requirement 1.1 validated at the boundary (`rfdetr_size ∈ {nano,small,medium,large}`, `resolution` a positive multiple of 56 in [224, 1120], `epochs` [1,1000], `batch` [1,64], `grad_accum` [1,64], `lr` in (0, 1), `patience` [0,1000], `score_threshold` in (0,1)), and `EnableNetworkIsolation = False` for the same pip/weights-download reason as YOLO.
3. THE Detection_Record_Fields for an RF-DETR job SHALL have `detection_arch = 'rf_detr'`, `network_input_width = network_input_height = resolution`, `preserve_aspect = False`, `score_threshold` (default 0.5), `top_k = 300`, and NO `iou_threshold`.
4. THE `TrainingHandler` Lambda asset SHALL bundle both entry points (`train.py`, `train_rfdetr.py`, `requirements.txt`, `requirements-rfdetr.txt`) plus the two converter files; `build_sourcedir_tarball` SHALL take the entry-point name and include only that entry point's requirements file so each job's `sourcedir.tar.gz` stays minimal.
5. THE default instance type for RF-DETR SHALL be `ml.g4dn.xlarge` (T4, 16 GB) with `batch = 4`, `grad_accum = 4` (the documented T4 configuration); `ml.g5.xlarge` SHALL be offered for `medium`/`large`.
6. THE `CreateTraining` Detection Settings panel SHALL show RF-DETR-specific inputs when the source is `rf_detr` (size, resolution defaulting to the size's native value, epochs, batch, gradient accumulation, learning rate, patience, score threshold) and SHALL NOT show the YOLO-only IoU threshold or base-weights dropdown.

### Requirement 4: Packaging writes the RF-DETR device manifest

**User Story:** As the packaging step, I want an RF-DETR-trained artifact to become a component the device decodes correctly, so that the two-tensor output and ImageNet normalisation are honoured.

#### Acceptance Criteria

1. `detection_training.build_detection_device_manifest` SHALL accept `detection_arch` and, for `'rf_detr'`, SHALL emit stage `type = 'rf_detr_object_detection'`, `normalize = True`, `output_shape = [1, 300, num_classes]` (nominal), and a `detection` block `{layout: 'rf_detr', num_classes, score_threshold, network_input, preserve_aspect: false, top_k: 300, class_names}` with NO `iou_threshold` — byte-equal to `model_converter.generate_dda_package(export_format='onnx', model_type='object_detection', detection_arch='rf_detr')`'s manifest (extend the parity test).
2. `packaging.package_trained_detection_component` SHALL read `detection_arch` from the record (falling back to `training_metadata.json`), nest `model.onnx` under the matching stage type directory, and prefer `training_metadata.json`'s `resolution` for the network input.
3. THE YOLO path SHALL remain byte-identical (existing `test_detection_training_compile_package.py` and parity tests keep passing).

### Requirement 5: Exploration spike — fine-tunable checkpoints in the wild

**User Story:** As the team, we want evidence about what "train on top of an imported model" can actually mean before we design it, so that the feature doesn't silently no-op on ONNX imports or crash on TorchScript.

#### Acceptance Criteria

1. THE spike SHALL enumerate, for every `TrainingJobs` record with `source='imported'` in the dev account and for the reference artifacts (`yolo-world-blue-plate`, the blue-plate v2 `best.pt`, an RF-DETR `checkpoint_best_total.pth`, a TorchScript `.pt`, an ONNX-only import), the answer to: *file kind (ONNX / TorchScript / ultralytics checkpoint / raw state_dict / RF-DETR pth), detectable how, loadable by which trainer, class count and class names recoverable?*
2. THE spike SHALL prototype `classify_checkpoint(path) -> {kind, arch, fine_tunable, num_classes, class_names, evidence}` as a pure function over file bytes/pickled metadata (no model execution), and record its precision on the enumerated set.
3. THE spike SHALL run one real fine-tune from a prior portal `best.pt` (YOLO) and one from `checkpoint_best_total.pth` (RF-DETR) on the blue-plate manifest and record mAP@50 vs. the published-checkpoint baseline, wall-clock, and any class-head reshaping needed when the class set changes.
4. THE spike SHALL decide and record: (a) whether ultralytics can load a `best.pt` whose class count differs from the new manifest (head re-init) and how RF-DETR handles the same; (b) whether Smart Import needs to start keeping the fine-tunable checkpoint alongside the ONNX it converts; (c) whether an imported *TorchScript* `.pt` should be rejected as a Base_Model (expected: yes).
5. THE findings SHALL be written to `docs/transfer-learning-spike.md`, and tasks 7.x of this spec SHALL be re-planned against them before implementation starts (design.md §Requirement 7 marks the parts that are provisional).

### Requirement 6: Base model selection in Create Training

**User Story:** As a Data Scientist, I want to choose a previous detector as the starting point and add new labeled data, so that each retrain builds on the last instead of restarting from COCO.

#### Acceptance Criteria

1. WHEN the Model Source is `yolo` or `rf_detr`, THE `CreateTraining` page SHALL show a **Base model** control offering: the published checkpoints for that arch (current behaviour, default), completed Detection_Training_Jobs in the use case with the same `detection_arch`, and Imported_Models whose record marks them `fine_tunable` for that arch (Requirement 7).
2. WHEN a prior Detection_Training_Job is chosen, THE page SHALL pre-fill class names from its record, SHALL default the network input to its `network_input_width`, and SHALL warn if the selected manifest's `class-map` differs from the base model's classes ("the class head will be re-initialised; expect more epochs").
3. WHEN `POST /training` receives `base_model = {kind, ref}`, THE Portal SHALL resolve it to a Base_Model_Descriptor: for `training_job` it SHALL locate the checkpoint inside that job's Detection_Artifact (`best.pt` / `checkpoint_best_total.pth`), verify the arch matches, and pass its S3 URI as `BASE_WEIGHTS_S3`; for `imported` it SHALL use the checkpoint recorded by Requirement 7; for `published` it SHALL pass the checkpoint name as today.
4. IF the base model's `detection_arch` differs from the requested one, or the referenced job is not `Completed`, or its artifact holds no Fine_Tunable_Checkpoint, THEN THE Portal SHALL return HTTP 400 naming the problem and SHALL create no SageMaker job.
5. THE entry points SHALL download `BASE_WEIGHTS_S3` (when set) to a local file and load it as `pretrain_weights` / `YOLO(<path>)`; the SageMaker execution role already reads the use-case bucket, and a base model from a *different* use case SHALL be rejected at the API (cross-tenant weights are out of scope).
6. THE Detection_Record_Fields SHALL persist `detection.base_model` (the Base_Model_Descriptor) so Training Detail can show "Fine-tuned from <name> v<version>" and so a chain of retrains is traceable.
7. THE `Imported Model (BYOM)` Model_Source entry SHALL be removed from the dropdown: "start from an imported model" is expressed by the Base model control under the arch that model belongs to, which is what the user actually means and avoids a source with no algorithm of its own.

### Requirement 7: Imported models as base models (provisional — shaped by Requirement 5)

**User Story:** As a Data Scientist, I want to bring a checkpoint I trained elsewhere and continue training it in the portal with my own labeled data.

#### Acceptance Criteria

1. WHEN Smart Import converts a `.pt`/`.pth` file, THE Portal SHALL run `classify_checkpoint` and persist on the Imported_Model record `metadata.fine_tunable = {arch, kind, checkpoint_s3}` when the file is a Fine_Tunable_Checkpoint, keeping the original checkpoint in the use-case bucket next to the converted package (today only the converted package survives).
2. WHEN an import is ONNX-only or TorchScript, THE record SHALL carry `metadata.fine_tunable = null` and the Base model control SHALL NOT offer it; Model Detail SHALL say why ("ONNX graphs cannot be fine-tuned; import the training checkpoint (.pt/.pth) to enable this").
3. THE Model Import page SHALL accept an optional **training checkpoint** upload alongside the ONNX for models that were converted outside the portal, so an existing ONNX deployment can be paired with its fine-tunable weights.
4. WHEN a Detection_Training_Job starts from an imported checkpoint whose class set differs from the manifest, THE entry point SHALL re-initialise the class head (per the spike's finding for each arch) and SHALL log the old and new class lists.
5. IF the spike (Requirement 5) finds that a format cannot be loaded reliably, THEN it SHALL be excluded here explicitly rather than best-effort loaded, and the exclusion SHALL be surfaced in the UI as in criterion 2.

### Requirement 8: Preservation, tests and verification

#### Acceptance Criteria

1. Every LFV path and the YOLO path SHALL stay byte-identical: the LFV preservation cases in `test_detection_training_create.py` and the YOLO detection cases SHALL pass without modification.
2. New tests SHALL cover: the converter `rfdetr` layout; RF-DETR hyperparameter validation; the RF-DETR `create_training_job` request shape (`train_rfdetr.py`, `RFDETR_SIZE`, `RESOLUTION`, no `IOU`); RF-DETR packaging manifest + parity with `generate_dda_package(detection_arch='rf_detr')`; base-model resolution (training_job / imported / published, arch mismatch → 400, cross-use-case → 400); `classify_checkpoint` on the spike's fixture set; frontend `trainingSources` (rf_detr enabled, byom removed) and the Base model control.
3. `packaging.py` (and `model_converter.py` if Requirement 7 touches it) SHALL be rebaselined in `iam_out_of_scope_baseline.json` in the same commit, with the note explaining why.
4. The full preservation suite SHALL be at or better than its pre-change baseline; the CDK-synth IAM baseline SHALL be unchanged (no new IAM actions: the SageMaker role already reads the use-case bucket where base-model checkpoints live).
5. AFTER deploy, an RF-DETR job on the blue-plate manifest and a YOLO job fine-tuned from the blue-plate v2 `best.pt` SHALL each be trained through the portal, packaged, published, deployed to the JP7 DLAP, and verified to detect with a healthy backend for a sustained period; results recorded in `docs/detection-training-gap.md`.
