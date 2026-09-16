# Transfer-learning spike — fine-tunable checkpoints in the wild

Spec: `.kiro/specs/rfdetr-training-and-transfer-learning/` (Requirement 5).
Status: **task 1.1 (inventory) done — 2026-09-13** (revised same day: added the
portal-trained job artifact R1′ and the verified RF-DETR `.pth` walk in §1.4);
**task 1.2 (`classify_checkpoint` prototype) done — 2026-09-14** (§2, 24/24 on
the §1 set; 25/25 with R2′ added by 1.3); **task 1.3 done — 2026-09-15**
(§3: seven fine-tune jobs `Completed` — YOLO and RF-DETR from published
weights and from their own checkpoints, plus the 1→2-class head tests for
both arches; one real `train_rfdetr.py` fix came out of it and was
re-verified on SageMaker, §3.3 (c-rfdetr)); **task 1.4 done — 2026-09-15**
(§4 decisions for Requirement 7, §5 the re-planned 7.x task list that
`tasks.md` §7.0 adopts).

This document is the evidence base for Requirement 7 ("imported models as base
models"). §1–§3 are evidence only; §4 holds the decisions (each citing the
evidence it rests on) and §5 the resulting task list.

Scratch area (not committed, ~2.8 GB): **`/tmp/tl-spike/`** on the build host
(WSL Ubuntu-22.04). Every artifact below is already downloaded and extracted
under `/tmp/tl-spike/artifacts/<label>/` so 1.2–1.3 must not re-download.
Helper scripts and raw logs live beside them (`01_scan.sh` … `15_rfdetr_tree.sh`,
`inspect_weights.py`, `deep_pickle.py`, `scripts/safe_pickle_tree.py`,
`inspect_results*.json`, `deep_pickle_results.json`, `safe_tree_results.json`,
`safe_tree_rfdetr.json`, `training_jobs_scan.json`, `meta/scan_all.json`).

### Scratch paths (for tasks 1.2 / 1.3)

| label | local path | what |
|---|---|---|
| I1 | `/tmp/tl-spike/artifacts/imp_blue_plate_custom_detector/extracted/export_artifacts/model.onnx` | imported ONNX |
| I2 | `/tmp/tl-spike/artifacts/imp_yolo_world_blue_plate_1e26701a/extracted/export_artifacts/model.onnx` | imported ONNX (`yolo-world-blue-plate` v2.0.0) |
| I3 | `/tmp/tl-spike/artifacts/imp_yolo_world_blue_plate_32962f3e/extracted/export_artifacts/model.onnx` | imported ONNX |
| I4 | `/tmp/tl-spike/artifacts/imp_yolo_test/extracted/export_artifacts/model.onnx` | imported ONNX (yolov8n COCO) |
| I5 | `/tmp/tl-spike/artifacts/imp_blue_plate_detector_v2/extracted/export_artifacts/model.onnx` | imported ONNX |
| I6 | `/tmp/tl-spike/artifacts/imp_yolo_world_blue_plate_v2/extracted/export_artifacts/model.onnx` | imported ONNX |
| I7 | `/tmp/tl-spike/artifacts/imp_rf_detr_seg_nano/extracted/export_artifacts/model.onnx` | imported ONNX (RF-DETR seg) |
| R1 | `/tmp/tl-spike/artifacts/ref_blue_plate_v2_yolo_job/extracted/best.pt` | ultralytics checkpoint, manual-launch job |
| R1′ | `/tmp/tl-spike/artifacts/ref_blue_plate_portal_job/extracted/best.pt` | ultralytics checkpoint, **portal** job `d62a831d…` (`models/training/blue-plate-*`) — use this for 1.3(a) |
| R2 | `/tmp/tl-spike/artifacts/ref_rfdetr_published_nano_pth/rf-detr-nano.pth` | RF-DETR `.pth` (published nano COCO) |
| R2′ | `/tmp/tl-spike/artifacts/ref_rfdetr_own_checkpoint/extracted/checkpoint_best_total.pth` | RF-DETR `.pth` from our own trainer (job `tl13-rfdetr-base-0506`, 1.3) — `model.tar.gz` and `extracted/{model.onnx,training_metadata.json}` beside it |
| R3 | `/tmp/tl-spike/artifacts/ref_lfv_cookies_binary_training_artifact/extracted/mochi.pt` | TorchScript |
| R4 | `/tmp/tl-spike/artifacts/ref_lfv_cookies_binary_training_artifact/extracted/mochi.pth` | plain `{state_dict: …}` |
| R5 | `/tmp/tl-spike/artifacts/ref_lfv_cookies_binary_training_artifact/extracted/checkpoints/resnet18-5c106cde.pth` | legacy torch tar |
| R6 | `/tmp/tl-spike/artifacts/raw_yolo_world_blue_plate_onnx/model.onnx` | raw ONNX |
| R7 | `/tmp/tl-spike/artifacts/raw_blue_plate_yolo_onnx/model.onnx`, `raw_rf_detr_base_coco_onnx/rf-detr-base-coco.onnx`, `raw_yolov8n_onnx/yolov8n.onnx` | raw ONNX |
| — | `/tmp/tl-spike/artifacts/ref_lfv_mochi_torchscript/extracted/` | Neo output (`compiled.so`, …) — negative case, not a checkpoint |
| — | `/tmp/tl-spike/rfdetr_pkg/src/rfdetr/` | `rfdetr` 1.10.1 wheel, unpacked (source of truth for the `.pth` contract) |

---

## 1. Inventory (task 1.1)

### 1.1 How the inventory was built

- Table: `dda-portal-training-jobs` (us-east-1, account 164152369890), 17
  records total. `aws dynamodb scan --filter-expression "#s = :imp"` with
  `source = imported` returns **7** records. All 7 belong to use case
  `645504ce-a60a-4009-8349-7548c0025cd3` (blue_plate / cookies dev use case).
- Each record's `artifact_s3` (a `converted-models/<name>-<hex>.tar.gz` DDA
  package) was downloaded with `aws s3 cp` and extracted. Every package has the
  same four members: `config.yaml`, `mochi.json`, `export_artifacts/manifest.json`,
  `export_artifacts/model.onnx`.
- The weights file inside each package (and each reference artifact) was
  classified by **inspection only** — header bytes, zip member names and a
  `pickletools.genops` opcode walk of the pickle envelope. Nothing was
  unpickled, no `torch` is installed on the host (`pip show torch` → not
  found). Scripts: `/tmp/tl-spike/inspect_weights.py` (kind by header/zip
  members/GLOBALs), `/tmp/tl-spike/deep_pickle.py` (key/value spotting),
  `/tmp/tl-spike/scripts/safe_pickle_tree.py` (literal-only pickle stack
  machine: rebuilds dict/list/scalar structure, turns GLOBAL/REDUCE/BUILD into
  opaque markers, never imports or calls anything).
- Detection rules used (these are the rules task 1.2 turns into
  `classify_checkpoint`):

  | Kind | Evidence |
  |---|---|
  | `onnx` | protobuf `ModelProto`: byte 0 = `0x08` (field 1 varint `ir_version`), field 2 string `producer_name`, then field 7 (`graph`) |
  | `torchscript_zip` | zip with `<root>/data.pkl` **and** `<root>/constants.pkl` **and** `<root>/code/…` ; `data.pkl` GLOBALs are all `__torch__.*` mangled class names |
  | `ultralytics_torch_checkpoint` | zip with `data.pkl` (no `constants.pkl`/`code/`); `data.pkl` GLOBALs include `ultralytics.nn.tasks.DetectionModel`, `ultralytics.nn.modules.head.Detect`; top-level keys `epoch, best_fitness, model, ema, updates, optimizer, train_args, …, version, license, docs` |
  | `rfdetr_pth` | zip with `data.pkl` (no `constants.pkl`/`code/`); **no framework module GLOBALs** (only `argparse.Namespace` / `collections.OrderedDict` / `torch.*Storage` / `torch._utils._rebuild_tensor_v2`); top-level keys `model` (raw state_dict) + `args` (`argparse.Namespace` or dict with `num_queries`, `group_detr`, `resolution`, `encoder`, optional `class_names`) (+ optional `epoch`, `model_name`, `model_config`, `callbacks`, `ema_model`); state_dict keys `class_embed.weight/bias`, `transformer.enc_out_class_embed.*`, `backbone.0.encoder.*` — see §1.4. **Distinguishing it from a plain state_dict needs the key-name check, not the GLOBAL list** |
  | `raw_state_dict_or_plain_dict` | zip with `data.pkl`; GLOBALs are only `collections.OrderedDict` + `torch.*Storage` + `torch._utils._rebuild_tensor_v2`; no `torch.nn` / framework module refs |
  | `legacy_torch_tar` | uncompressed tar starting `././@PaxHeader`, members `sys_info`, `pickle`, `tensors`, `storages` (pre-`torch` 0.4 / `_use_new_zipfile_serialization=False` era) |

### 1.2 Imported records (`source = 'imported'`) — 7 of 7 are ONNX-only

| # | `model_name` (`training_id`) | `model_type` | `artifact_s3` | weights file in package | kind | detected by | loadable by trainer | classes recoverable? | local path |
|---|---|---|---|---|---|---|---|---|---|
| I1 | `blue_plate_custom_detector` (`acbfb2d4-7faa-49f1-b4e2-28944f8a3e34`) | `yolo_object_detection` | `s3://ryvan-cookies/converted-models/blue_plate_custom_detector-86948305.tar.gz` | `export_artifacts/model.onnx` (38,411,162 B) | **ONNX** | `ModelProto` header, `ir_version 10`, producer `pytorch 2.5.1` | **none** — not fine-tunable | `config.yaml` → `num_classes: 1`, `class_names: [blue_plate]`; graph output `[1,5,8400]` → 4+1 | `/tmp/tl-spike/artifacts/imp_blue_plate_custom_detector/` |
| I2 | `yolo-world-blue-plate` v2.0.0 (`8c81dd90-cde8-4f6f-a88f-779009260bd6`) — **the `yolo-world-blue-plate` reference ONNX** | `yolo_object_detection` | `s3://ryvan-cookies/converted-models/yolo_world_blue_plate-1e26701a.tar.gz` | `export_artifacts/model.onnx` (185,872,258 B) | **ONNX** | header, `ir_version 10`, producer `pytorch 2.13.0+cpu` | **none** | `config.yaml` → `num_classes: 1`, `class_names: ["blue box"]` | `/tmp/tl-spike/artifacts/imp_yolo_world_blue_plate_1e26701a/` |
| I3 | `yolo-world-blue-plate` v1.0.0 (`5dd208c4-081e-44eb-9e96-ed53f012a2b3`) | `yolo_object_detection` | `s3://ryvan-cookies/converted-models/yolo_world_blue_plate-32962f3e.tar.gz` | `export_artifacts/model.onnx` (185,569,362 B) — byte-identical to `raw-models/yolo-world-blue-plate/model.onnx` | **ONNX** | header, `ir_version 10`, producer `pytorch 2.13.0+cpu` | **none** | `config.yaml` → `num_classes: 1` (no `class_names`) | `/tmp/tl-spike/artifacts/imp_yolo_world_blue_plate_32962f3e/` |
| I4 | `yolo_test` (`6a43ff2b-50ec-4441-b5f0-d84ccfef1400`) | `yolo_object_detection` | `s3://ryvan-cookies/converted-models/yolo_test-5aae1c82.tar.gz` | `export_artifacts/model.onnx` (12,821,996 B) — byte-identical to `models/yolov8n.onnx` | **ONNX** | header, `ir_version 8`, producer `pytorch 1.13.0` | **none** | `config.yaml` → `num_classes: 80` (no names; COCO by convention) | `/tmp/tl-spike/artifacts/imp_yolo_test/` |
| I5 | `blue_plate_detector_v2` (`425f5e49-d074-46f9-b206-78543fc87fed`) | `yolo_object_detection` | `s3://ryvan-cookies/converted-models/blue_plate_detector_v2-e4aa3de5.tar.gz` | `export_artifacts/model.onnx` (38,411,162 B) — byte-identical to the `blue-plate-yolo-20260912-231818` job's `model.onnx` (R1) | **ONNX** | header, `ir_version 10`, producer `pytorch 2.5.1` | **none** — the portal-trained `best.pt` for this model exists only in the *training job's* artifact (R1), not in the import | `config.yaml` → `num_classes: 1` (no `class_names` — the "label says `person`" bug in `docs/blue-plate-retrain-handoff.md`) | `/tmp/tl-spike/artifacts/imp_blue_plate_detector_v2/` |
| I6 | `yolo-world-blue-plate-v2` (`079c0a3c-11a0-40f9-b921-6dd4797fc888`) | `yolo_object_detection` | `s3://ryvan-cookies/converted-models/yolo_world_blue_plate_v2-d4110850.tar.gz` | `export_artifacts/model.onnx` (186,193,660 B) | **ONNX** | header, `ir_version 7`, producer `pytorch 2.4.1` | **none** | `config.yaml` → `num_classes: 1` | `/tmp/tl-spike/artifacts/imp_yolo_world_blue_plate_v2/` |
| I7 | `rf-detr-seg-nano` (`ede4bd32-e12c-4e88-8139-75d622e53ad0`) | `segmentation` | `s3://ryvan-cookies/converted-models/rf_detr_seg_nano-a9fd2feb.tar.gz` | `export_artifacts/model.onnx` (122,610,239 B) | **ONNX** | header, `ir_version 8`, producer `pytorch 2.8.0` | **none** | `config.yaml` → `num_classes: 91`; manifest `pixel_level_classes.names` are placeholders `class_0…class_90` | `/tmp/tl-spike/artifacts/imp_rf_detr_seg_nano/` |

Observations that matter for Requirement 7:

- **Every import in the dev account is ONNX-only.** Each record's
  `metadata.framework = 'ONNX'`, `metadata.pt_file = 'model.onnx'`, and the
  `description` names a bare `.onnx` source (`raw-models/…/model.onnx`,
  `models/yolov8n.onnx`, `models/rf-detr-seg-nano.onnx`). No `.pt`/`.pth` was
  ever imported through Smart Import here, so there is no in-account evidence
  of what a *checkpoint* import produces — Smart Import's `.pt` path
  (`model_converter.inspect_pytorch_model`) is exercised only by the reference
  artifacts below.
- **There is no `.pt`/`.pth` anywhere in `s3://ryvan-cookies`** (1,233 keys
  listed; `grep -E '\.(pt|pth)$'` → 0 hits; `checkpoint` → 0 hits). Same for
  `dda-portal-artifacts-…`, `bd-dda`, `rajjainl-dda-lfv-cv-bucket`,
  `ryvan-dda-component`, `lfv-s3-bucket-ryvan-100725`, `manufacturing-line-1`.
  The only fine-tunable checkpoints in the account are *inside tarballs*:
  the portal-trained YOLO job's `best.pt` (R1) and LFV training artifacts
  (R3/R4/R5, which are not detectors).
- Class names survive an ONNX import only if the user typed them: `config.yaml`
  carries `class_names` for I1 and I2 and nothing for I3–I7. The ONNX graph
  itself yields `num_classes` (from the output shape) but never names.
- Two records (I1, I5) wrap the **same** ONNX bytes as the portal training job
  R1 — i.e. the fine-tunable `best.pt` for those imports *does* exist, but in
  a different S3 object (`models/detection/<job>/output/.../model.tar.gz`)
  that no record links to. This is the concrete case Requirement 6.3
  (`kind = 'training_job'`) covers without needing Requirement 7 at all.

### 1.3 Reference artifacts

| # | Name | S3 source | files in tarball | weights file | kind | detected by | loadable by trainer | classes recoverable? | local path |
|---|---|---|---|---|---|---|---|---|---|
| R1 | **blue-plate v2 `best.pt`** — SageMaker job `blue-plate-yolo-20260912-231818` (the portal-trained YOLO whose ONNX became I5/I1) | `s3://ryvan-cookies/models/detection/blue-plate-yolo-20260912-231818/output/blue-plate-yolo-20260912-231818/output/model.tar.gz` (50,185,561 B, sha256 `4bc55a0d…790ec9`) | `model.onnx`, `training_metadata.json`, `best.pt` | `best.pt` (19,307,731 B) | **ultralytics torch checkpoint** (full pickled model) | zip root `best/`; `data.pkl` + `byteorder` + `version` + 503 `data/N` storages; **no** `constants.pkl`/`code/`; pickle proto 2; GLOBALs include `ultralytics.nn.tasks.DetectionModel`, `ultralytics.nn.modules.head.Detect`, `…block.C3k2`, `…block.C2PSA`, `…block.DFL`; top-level keys `date, version (8.3.40), license (AGPL-3.0), docs, epoch, best_fitness, model, …` | **ultralytics `YOLO(path)`** — yes (this is exactly what `train.py`'s `BASE_WEIGHTS` consumes) | **yes** from the pickle: `model.names` `{0: …}` and `model.yaml['nc']` are in `data.pkl` (see §1.4 / `deep_pickle_results.json`). `training_metadata.json` does **not** carry `class_names` today (only `imgsz, base_weights, epochs, opset, onnx_output_shape [1,5,33600], metrics, device_manifest_hints`) — so the checkpoint is the *only* source of names for a portal-trained YOLO | `/tmp/tl-spike/artifacts/ref_blue_plate_v2_yolo_job/extracted/best.pt` |
| R1′ | **blue-plate `best.pt` from the *portal* job** — record `d62a831d-c453-4a00-b4b2-98ea12a67f0b` (`blue-plate-trained-imts`, `source` unset, `model_type=object_detection`, `runtime=onnx`, `detection.detection_arch=yolo`, `detection.class_names=[blue_plate]`). This is the artifact a Req 6.3 `kind='training_job'` base model would point at (`models/training/blue-plate-*/output/model.tar.gz`) | `s3://ryvan-cookies/models/training/blue-plate-trained-imts-20260913-142306/blue-plate-trained-imts-20260913-142306/output/model.tar.gz` (50,184,727 B, sha256 `2b4e1897…96341f`) | `best.pt`, `training_metadata.json`, `model.onnx` | `best.pt` (19,307,731 B, sha256 `27e927d4…eb5f62`; **different bytes** from R1 — a separate run, same recipe) | **ultralytics torch checkpoint** | identical envelope to R1: zip root `best/`, `data.pkl` + `byteorder` + `version` + 503 storages, no `constants.pkl`/`code/`; GLOBAL `ultralytics.nn.tasks.DetectionModel`; ultralytics `8.3.40`, `date 2026-09-13T14:42:26` | **ultralytics `YOLO(path)`** — yes | **yes** — safe literal walk (`safe_tree_results.json`): `model.<state>.names = {0: 'blue_plate'}`, `model.<state>.nc = 1`, `model.<state>.yaml = {nc: 1, scale: 's', yaml_file: 'yolo11s.yaml', …}`, `train_args.imgsz = 1280`, `train_args.model = 'yolo11s.pt'`. Its `model.onnx` (sha256 `bb39a140…8846bd`) matches **no** import — the portal job was never Smart-Imported | `/tmp/tl-spike/artifacts/ref_blue_plate_portal_job/extracted/best.pt` |
| R2 | **RF-DETR `.pth`** — *stand-in*: the published COCO **nano** checkpoint that `rfdetr` 1.10.1 itself downloads for `RFDETRNano` (`rfdetr/assets/model_weights.py::RF_DETR_NANO`, md5 `fb6504cce7fbdc783f7a46991f07639f` — **verified** after download) | `https://storage.googleapis.com/rfdetr/nano_coco/checkpoint_best_regular.pth` (public; **no** `.pth` exists in the AWS account) | bare file | `rf-detr-nano.pth` (366,287,238 B, sha256 `d8d6b9ee…57440d`) | **RF-DETR `.pth`** (`{model, optimizer, lr_scheduler, epoch, args}` torch zip) | zip root `checkpoint_best_regular/`; `data.pkl` + `byteorder` + `version` + `.format_version` + `.storage_alignment` + 1,857 storages; **no** `constants.pkl`/`code/`; pickle proto 2; GLOBALs are **only** `argparse.Namespace`, `collections.OrderedDict`, `torch.FloatStorage`, `torch._utils._rebuild_tensor_v2` — **no `rfdetr.*`/`lwdetr.*` module reference anywhere** (the design.md heuristic "pickles reference `rfdetr`/`lwdetr` module paths" is *wrong* for this file; see §1.4). Positive signals: top-level `args` is an `argparse.Namespace` (118 fields incl. `encoder='dinov2_windowed_small'`, `num_queries=300`, `group_detr=13`, `resolution=384`, `two_stage`, `dec_layers`, `hidden_dim`) and `model` is a raw state_dict with `class_embed.weight [91,256]`, `class_embed.bias [91]`, `transformer.enc_out_class_embed.{0..12}.*`, `backbone.0.encoder.encoder.encoder.layer.*` | **`rfdetr` `RFDETR*(pretrain_weights=<path>)`** — yes; this is the same envelope `BestModelCallback` writes for `checkpoint_best_regular.pth` / `checkpoint_best_ema.pth` / `checkpoint_best_total.pth` (§1.4) | **partly**: `num_classes` = `class_embed.bias.shape[0] − 1` = **90** is reliable (this is exactly what `load_pretrain_weights` uses). `args.class_names` is **absent** in the published file (COCO names come from the package). `args.num_classes = 2` is a stale CLI default and must **not** be trusted — the head shape wins. For our own `checkpoint_best_total.pth` the callback back-fills `args.class_names` from the datamodule and syncs `model_config.num_classes` from `class_embed.weight`, so R2′ will carry names | `/tmp/tl-spike/artifacts/ref_rfdetr_published_nano_pth/rf-detr-nano.pth` |
| R2′ | **`checkpoint_best_total.pth` from our own trainer** | — | — | — | RF-DETR `.pth` | — | — | — | **to be produced by task 1.3** (RF-DETR baseline run on the blue-plate manifest). Format is pinned by the package source (§1.4), so 1.2 can be written against R2 and re-checked on R2′ |
| R3 | **TorchScript `.pt`** — LFV `mochi.pt` from the `cookies-binary` training artifact | `s3://ryvan-cookies/models/training/cookies-binary-20260607-024610/cookies-binary-20260607-024610/output/model.tar.gz` (250,298,922 B, sha256 `bd46002b…5f411df`) | `mochi.pt`, `mochi.pth`, `mochi.json`, `config.yaml`, `checkpoints/resnet18-5c106cde.pth`, `export_artifacts/{manifest.json, mochi.pt}`, `evaluation/`, `inference/`, `anomaly_masks/` | `mochi.pt` (44,869,864 B; `export_artifacts/mochi.pt` is byte-identical) | **TorchScript zip** | zip root `mochi/`; `data.pkl` **+ `constants.pkl` + `code/__torch__/…py` (+ `.debug_pkl`)**; every GLOBAL is a `__torch__.…` mangled class (`__torch__.lyra_science.supervised_anomaly_detection.models.multi_head_model.MultiHeadModel`, `__torch__.segmentation_models_pytorch.encoders.resnet.ResNetEncoder`, …) | **none** — `torch.jit.load` gives a frozen graph; neither ultralytics nor rfdetr can continue training it | **no** — no `names`/`nc` in the pickle; only `mochi.json` beside it knows the task | `/tmp/tl-spike/artifacts/ref_lfv_cookies_binary_training_artifact/extracted/mochi.pt` |
| R4 | *(bonus, same tarball)* LFV `mochi.pth` | as R3 | as R3 | `mochi.pth` (134,255,774 B) | **plain dict / raw `state_dict`** | zip root `archive/`; `data.pkl` only; GLOBALs = `collections.OrderedDict`, `torch.{Float,Double,Long}Storage`, `torch._utils._rebuild_tensor_v2`; first string `state_dict`, then keys `encoder.conv1.weight`, … ; the only non-tensor value is `anomaly_score_threshold` | **none** for our two trainers (no architecture definition; keys are `encoder.*` = a ResNet18 + heads) | **no** | `/tmp/tl-spike/artifacts/ref_lfv_cookies_binary_training_artifact/extracted/mochi.pth` |
| R5 | *(bonus, same tarball)* torchvision `resnet18-5c106cde.pth` | as R3 | as R3 | `checkpoints/resnet18-5c106cde.pth` (46,827,520 B) | **legacy torch tar** | magic `././@PaxHeader` — an uncompressed tar with `sys_info`, `pickle`, `tensors`, `storages` (pre-zip `torch.save`) | **none** | **no** | `/tmp/tl-spike/artifacts/ref_lfv_cookies_binary_training_artifact/extracted/checkpoints/resnet18-5c106cde.pth` |
| R6 | **`yolo-world-blue-plate` ONNX** (the record's raw source) | `s3://ryvan-cookies/raw-models/yolo-world-blue-plate/model.onnx` (185,569,362 B, sha256 `0313324b…fc4c7`) | bare file | `model.onnx` | **ONNX** | header, `ir_version 10`, producer `pytorch 2.13.0+cpu` | **none** | `num_classes` from output shape only | `/tmp/tl-spike/artifacts/raw_yolo_world_blue_plate_onnx/model.onnx` |
| R7 | ONNX-only import sources (raw) | `s3://ryvan-cookies/raw-models/blue-plate-yolo/model.onnx` (sha256 `1a1144c4…33dbb`, matches `docs/blue-plate-retrain-handoff.md`), `s3://ryvan-cookies/models/rf-detr-base-coco.onnx`, `s3://ryvan-cookies/models/yolov8n.onnx` | bare files | `.onnx` | **ONNX** | header (`ir_version` 10 / 8 / 8; producer `pytorch` 2.5.1 / 2.8.0 / 1.13.0) | **none** | shape only | `/tmp/tl-spike/artifacts/raw_blue_plate_yolo_onnx/`, `raw_rf_detr_base_coco_onnx/`, `raw_yolov8n_onnx/` |

Not a weights file but worth recording: the LFV *compilation* repackage
`models/compilation/<job>/jetson-xavier/model_for_compilation-LINUX_ARM64_NVIDIA.tar.gz`
(46 MB) is Neo **output** (`compiled.so`, `compiled.params`, `libdlr.so`,
`manifest`), not the `mochi.pt` repackage its name suggests
(`/tmp/tl-spike/artifacts/ref_lfv_mochi_torchscript/`). A classifier will see
ELF shared objects there; that is a "not a checkpoint" case, not a kind.

### 1.4 What the pickle envelopes actually contain (evidence for 1.2)

Ultralytics `best.pt` (R1), from `data.pkl` opcode walk (`deep_pickle_results.json`):

- Top-level dict keys in order: `date`, `version`, `license`, `docs`, `epoch`,
  `best_fitness`, `model`, `ema`, `updates`, `optimizer`, `train_args`,
  `train_metrics`, `train_results` (standard `ultralytics.engine.trainer`
  `save_model()` layout; `version = '8.3.40'`).
- `model` is a *pickled `DetectionModel` object* (GLOBAL
  `ultralytics.nn.tasks.DetectionModel`, then `torch.nn.modules.*` and
  `ultralytics.nn.modules.*` for every layer). Its attributes `names`
  (`{0: '<name>'}`), `yaml` (`{'nc': 1, …}`), `stride`, `args` are all plain
  dict/str/int literals in the pickle and can be pulled out **without**
  unpickling. Literal walk of R1 and R1′ (`safe_tree_results.json`) — both
  identical in shape, 53,938 opcodes each:
  - top-level: `date, version ('8.3.40'), license, docs, epoch (-1),
    best_fitness (None), model (REDUCE DetectionModel), ema (None),
    updates (None), optimizer (None), train_args (dict), train_metrics,
    train_results` — `best.pt` is the *stripped* checkpoint (`ema`/`optimizer`
    nulled by `strip_optimizer`), so `model` is the only weights carrier.
  - `model.<state>` keys: `training, _parameters, _buffers, …, _modules,
    yaml, save, names, inplace, end2end, stride, nc, args, criterion`.
  - `model.<state>.names = {0: 'blue_plate'}`, `nc = 1`,
    `yaml = {nc: 1, scale: 's', yaml_file: 'yolo11s.yaml', ch: 3, backbone:
    […], head: […Detect]}`, `end2end = False`, `stride = <tensor [3]>`.
  - `train_args`: `task='detect', model='yolo11s.pt', imgsz=1280, epochs=100,
    batch=4, patience=30, data='/opt/ml/input/work/dataset/data.yaml'` — the
    whole launch recipe is recoverable from the file.
  - So for an ultralytics checkpoint `num_classes`, `class_names`, the base
    checkpoint and `imgsz` are all recoverable by inspection; the `Detect`
    head shape (`model.23.cv3.*`) is *not* needed.
- Consequence for Smart Import today: `model_converter.inspect_pytorch_model`
  loads with `weights_only=True` first, which **fails** on this file (it needs
  the `ultralytics.*` and `torch.nn.*` globals), then falls back to
  `weights_only=False` only for allow-listed trusted buckets. So a `best.pt`
  from a non-trusted bucket cannot even be inspected by the current converter.

RF-DETR `.pth` (R2), from the package source (`rfdetr` 1.10.1 wheel unpacked
at `/tmp/tl-spike/rfdetr_pkg/src/`) and the file walk:

- `rfdetr/training/callbacks/best_model.py::BestModelCallback` writes
  `checkpoint_best_regular.pth`, `checkpoint_best_ema.pth`, `last.pth`,
  `last_ema.pth`, and copies the winner to **`checkpoint_best_total.pth`**.
  Payload (`_build_checkpoint_payload`): `model` (raw-keyed state_dict),
  `args` (the train config `model_dump()`, with `class_names` back-filled
  from the datamodule), `epoch`, `model_name` (e.g. `RFDETRSmall`),
  `model_config` (with `num_classes` synced from `class_embed.weight.shape[0]-1`),
  `callbacks` (PTL callback states), and for EMA files `ema_model`. Optimizer
  and scheduler state are stripped.
- `rfdetr/models/weights.py::load_pretrain_weights` is what
  `RFDETR*(pretrain_weights=…)` calls (both the L1 facade and the Lightning
  module). It: (1) `_safe_torch_load` with `weights_only=True`, then with
  `argparse.Namespace`/`SimpleNamespace` allow-listed, then full pickle only
  if `trust=True`; (2) normalises PTL `.ckpt` (`state_dict` with `model.`
  prefix → `model`; `hyper_parameters` → `args`); (3) **returns
  `args.class_names`**; (4) reads `checkpoint_num_classes =
  checkpoint["model"]["class_embed.bias"].shape[0]` and, when it differs from
  the configured `num_classes + 1`, calls
  `nn_model.reinitialize_detection_head(...)` — if the user did not set
  `num_classes` the checkpoint's count wins; if the user did, the head is
  aligned to the checkpoint for loading and then re-initialised to the
  configured size. **So RF-DETR handles a class-count change by design; 1.3
  verifies it end-to-end.** (`load_state_dict(strict=False)` still raises on
  same-key shape mismatches, which is why the head is resized first.)
- Published weight names → URLs (all Google Cloud Storage, MD5-pinned in
  `model_weights.py`): `rf-detr-nano.pth` → `nano_coco/checkpoint_best_regular.pth`,
  `rf-detr-small.pth` → `small_coco/checkpoint_best_regular.pth`,
  `rf-detr-medium.pth` → `medium_coco/checkpoint_best_regular.pth`,
  `rf-detr-large.pth`, `rf-detr-base.pth` (= `rf-detr-base-coco.pth`).
  Segmentation weights are `.pt`-named but `.pth` URLs (`rf-detr-seg-n-ft.pth` …).
- File-level walk of R2 (`scripts/safe_pickle_tree.py` — a literal-only pickle
  stack machine over `pickletools.genops`; GLOBAL/REDUCE/BUILD become opaque
  nodes, nothing is imported or called; results in `safe_tree_rfdetr.json`):
  - top-level keys, in order: `model`, `optimizer`, `lr_scheduler`, `epoch`
    (= 48), `args`. This is the *older* training-loop layout (optimizer +
    scheduler still present, no `model_name`/`model_config`/`callbacks`);
    `BestModelCallback` in 1.10.1 strips optimizer/scheduler and adds
    `model_name`, `model_config`, `callbacks`. A classifier must accept
    **both** layouts (`model` + `args` is the common core).
  - `model` is a `collections.OrderedDict` (REDUCE) with 465 tensor entries;
    `class_embed.weight [91, 256]`, `class_embed.bias [91]` → `num_classes =
    90` (+1 background) — the published nano is the COCO head.
    `transformer.enc_out_class_embed.{0..12}.{weight,bias}` (13 = `group_detr`).
  - `args` is an `argparse.Namespace` (REDUCE + BUILD state) with 118 fields.
    Relevant: `encoder = 'dinov2_windowed_small'`, `resolution = 384`,
    `num_queries = 300`, `group_detr = 13`, `dec_layers = 2`,
    `hidden_dim = 256`, `two_stage = True`, `dataset_file = 'coco'`,
    `resume = 'rf-detr-nano-experimental.pth'`. **`class_names` is absent**
    and **`num_classes = 2`** — a leftover CLI default that contradicts the
    91-wide head. `load_pretrain_weights` ignores `args.num_classes` and
    reads `class_embed.bias.shape[0]`; a classifier must do the same.
  - GLOBALs (complete list): `argparse.Namespace`, `collections.OrderedDict`,
    `torch.FloatStorage`, `torch._utils._rebuild_tensor_v2`. **Nothing
    references the `rfdetr`/`lwdetr` packages**, so the design.md sketch
    ("RF-DETR `.pth` pickles reference `rfdetr`/`lwdetr` module paths") does
    not hold for real files; `inspect_weights.py`'s first-pass heuristic
    accordingly misfiled R2 as `raw_state_dict_or_plain_dict`. Correct
    signal = `args` namespace fields + `class_embed.*` / `enc_out_class_embed.*`
    / `backbone.0.encoder.*` key names.
  - Loading path for R2 in `rfdetr` 1.10.1: `_safe_torch_load` step 1
    (`weights_only=True`) **fails** on the `argparse.Namespace`; step 2
    (same, with `argparse.Namespace`/`SimpleNamespace` allow-listed via
    `torch.serialization.safe_globals`) succeeds. Our own
    `checkpoint_best_total.pth` (R2′) stores `args` as a plain dict
    (`train_config.model_dump()`), so it loads at step 1.

TorchScript `mochi.pt` (R3): the `data.pkl` GLOBAL list is *entirely*
`__torch__.<module>.<Class>` (plus `___torch_mangle_N` variants) and the zip
carries `constants.pkl` + `code/`. Those two facts are sufficient and
mutually confirming; no framework module names appear. `torch.jit.load` is
the only loader; it cannot be fine-tuned by either trainer.

Plain `{state_dict: …}` (R4) and legacy tar (R5) round out the negative set:
no framework GLOBALs, no `names`/`args`, no head to reshape — a classifier
must return `fine_tunable = False`, `arch = None` for them.

### 1.5 Inventory summary

| kind | count | which |
|---|---|---|
| ONNX (not fine-tunable) | 7 imported + 6 raw/ref (`R6`, `R7`×3, R1's and R1′'s `model.onnx`) | all 7 `source='imported'` records |
| ultralytics torch checkpoint (fine-tunable, YOLO) | 2 | R1 `best.pt` (manual job), R1′ `best.pt` (portal job `d62a831d…`) |
| RF-DETR `.pth` (fine-tunable, RF-DETR) | 1 stand-in (R2) + R2′ pending 1.3 | published `rf-detr-nano.pth` (older `{model, optimizer, lr_scheduler, epoch, args}` layout) |
| TorchScript zip (not fine-tunable) | 1 | R3 `mochi.pt` |
| plain dict / raw `state_dict` (not fine-tunable) | 1 | R4 `mochi.pth` |
| legacy torch tar (not fine-tunable) | 1 | R5 `resnet18-5c106cde.pth` |

Gaps carried forward:

- No RF-DETR `checkpoint_best_total.pth` exists in the account → **task 1.3
  produces R2′**; until then R2 (same envelope, written by the same callback
  class) is the fixture for 1.2.
- No imported `.pt`/`.pth` exists in the account, so "what does Smart Import
  do with a checkpoint" has to be answered from code (`model_converter.py`)
  plus R1/R3 rather than from a record.
- `train.py` does not write `class_names` into `training_metadata.json`
  (confirmed on R1′: keys are `imgsz, base_weights, epochs, opset,
  onnx_output_shape, metrics, manifest_s3, images_s3, device_manifest_hints`);
  for Requirement 6.2 (pre-fill class names from a prior job) the *record*
  already has `detection.class_names` (R1′'s record: `[blue_plate]`), so the
  portal path is covered — only the *imported checkpoint* path (Req 7) needs
  names from the file. A 1.4 decision whether the metadata should gain
  `class_names` anyway.
- **Design correction carried to 1.2**: the RF-DETR `.pth` cannot be told
  apart from a plain state_dict by pickle GLOBALs (none of `rfdetr`/`lwdetr`
  appear). `classify_checkpoint` must key on the state_dict member names
  (`class_embed.*`, `transformer.enc_out_class_embed.*`, `backbone.0.encoder.*`)
  and the `args` field set (`num_queries`, `group_detr`, `encoder`,
  `resolution`), and must read `num_classes` from `class_embed.bias` length −1,
  never from `args.num_classes`.
- Two checkpoint layouts of RF-DETR `.pth` are in the wild — the published
  files (`{model, optimizer, lr_scheduler, epoch, args}` with `args` as
  `argparse.Namespace`, no `class_names`) and the 1.10.1 callback output
  (`{model, args(dict), epoch, callbacks, model_name, model_config[, ema_model]}`
  with `args.class_names`). 1.2 must handle both; 1.3 produces the second.

---

## 2. `classify_checkpoint` prototype (task 1.2)

Status: **done — 2026-09-14.** Module:
`datasets/detection_training/_checkpoint_probe.py` (stdlib only, no `torch`;
`python3 -m py_compile` clean). **Moved by task 7.1 to
`edge-cv-portal/backend/layers/shared/python/checkpoint_probe.py`** (behaviour
unchanged; re-exported from `detection_training.py`; the prototype path below
no longer exists). Run over the §1 set with
`python3 datasets/detection_training/_checkpoint_probe.py <paths…>` (one JSON
object per file); the sweep below was driven by a throw-away loop over the
"Scratch paths" table plus the Neo-output negative set. Raw results were kept
at `/tmp/probe_sweep.json` (scratch, not committed).

### 2.1 Contract (as implemented)

`classify_checkpoint(path) -> {kind, arch, fine_tunable, num_classes,
class_names, evidence}` with `kind ∈ {onnx, torchscript,
ultralytics_checkpoint, rfdetr_checkpoint, state_dict, legacy_torch,
unknown}`, `arch ∈ {yolo, rf_detr, None}`, `fine_tunable = True` only for
`ultralytics_checkpoint` / `rfdetr_checkpoint`. It **never raises**: a 0-byte
file, 1 KiB of `/dev/urandom`, a zip without `data.pkl`, a truncated `PK…`
header, a truncated `\x80\x02` pickle and a non-existent path all come back
as `kind='unknown'` with the failure under `evidence.error` /
`evidence.note` / `evidence.pickle_error` (checked in this run). The only
"garbage" case that is not `unknown` is a bare 8-byte ONNX header
(`08 0a 12 07 pytorch`), which is reported as `onnx` with no outputs — that is
the correct reading of those bytes.

Mechanics match the §1.1 rules: ONNX by `ModelProto` header + a
seek-over-blobs scan that reads only `opset_import`, graph `output` and
`metadata_props` (initializers are never read, so a 186 MB ONNX takes
< 10 ms); torch zips by member set (`data.pkl` / `constants.pkl` / `code/`)
plus a literal-only `pickletools.genops` stack machine over `data.pkl`
(GLOBAL/REDUCE/NEWOBJ/BUILD become opaque nodes, nothing is imported or
called); legacy tar by member names. `num_classes` for RF-DETR is read from
the `class_embed.bias` `_rebuild_tensor_v2` size argument (`shape[0] − 1`),
never from `args.num_classes`, as §1.4 requires.

### 2.2 Sweep over the §1 set (24 files: 17 positives + 7 Neo-output negatives)

| label | path (under `/tmp/tl-spike/artifacts/`) | expected kind (§1) | predicted kind / arch | `num_classes` | `class_names` | source of names / count | ok |
|---|---|---|---|---|---|---|---|
| I1 | `imp_blue_plate_custom_detector/…/model.onnx` | onnx | `onnx` / `yolo` | 1 | `[blue_plate]` | `metadata_props.names` (ir 10, pytorch 2.5.1, out `[1,5,33600]`) | ✓ |
| I2 | `imp_yolo_world_blue_plate_1e26701a/…/model.onnx` | onnx | `onnx` / `yolo` | 1 | `["blue box"]` | `metadata_props.names` (ir 10, pytorch 2.13.0+cpu, out `[1,5,33600]`) | ✓ |
| I3 | `imp_yolo_world_blue_plate_32962f3e/…/model.onnx` | onnx | `onnx` / `yolo` | 1 | `["blue plate"]` | `metadata_props.names` (ir 10, out `[1,5,8400]`) | ✓ |
| I4 | `imp_yolo_test/…/model.onnx` | onnx | `onnx` / `yolo` | 80 | 80 COCO names (`person`…`toothbrush`) | `metadata_props.names` (ir 8, pytorch 1.13.0, out `[1,84,8400]`) | ✓ |
| I5 | `imp_blue_plate_detector_v2/…/model.onnx` | onnx | `onnx` / `yolo` | 1 | `[blue_plate]` | `metadata_props.names` (byte-identical to R7a) | ✓ |
| I6 | `imp_yolo_world_blue_plate_v2/…/model.onnx` | onnx | `onnx` / `yolo` | 1 | `["small blue rectangular object"]` | `metadata_props.names` (ir 7, pytorch 2.4.1) | ✓ |
| I7 | `imp_rf_detr_seg_nano/…/model.onnx` | onnx | `onnx` / `None` | — | — | no `metadata_props`; 3 outputs, as named in the graph: `pred_masks [1,100,4]`, `pred_boxes [1,100,91]`, `pred_logits [1,<dyn>,78,78]` (names look permuted against shapes in the exporter; the probe reports what the file says) | ✓ |
| R1 | `ref_blue_plate_v2_yolo_job/extracted/best.pt` | ultralytics_checkpoint | `ultralytics_checkpoint` / `yolo`, fine_tunable | 1 | `[blue_plate]` | `model.<state>.names` (+ `nc`, `yaml_file: yolo11s.yaml`, `train_args {task: detect, model: yolo11s.pt, imgsz: 1280, epochs: 100, batch: 4}`, ultralytics `8.3.40`; 53,938 opcodes) | ✓ |
| R1′ | `ref_blue_plate_portal_job/extracted/best.pt` | ultralytics_checkpoint | `ultralytics_checkpoint` / `yolo`, fine_tunable | 1 | `[blue_plate]` | as R1 (`date 2026-09-13T14:42:26`) | ✓ |
| R2 | `ref_rfdetr_published_nano_pth/rf-detr-nano.pth` | rfdetr_checkpoint | `rfdetr_checkpoint` / `rf_detr`, fine_tunable | **90** | **none** | `class_embed.bias.shape[0] − 1` (= 91 − 1); signals: `class_embed.*` ✓, `enc_out_class_embed` groups = 13, 223 `backbone.0.encoder.*` keys, `args` = `argparse.Namespace` with `num_queries 300 / group_detr 13 / encoder dinov2_windowed_small / resolution 384`; stale `args.num_classes = 2` correctly ignored; `epoch 48`; top-level `{model, optimizer, lr_scheduler, epoch, args}` | ✓ |
| R3 | `ref_lfv_cookies_binary_training_artifact/extracted/mochi.pt` | torchscript | `torchscript` / `None` | — | — | `constants.pkl` + `code/__torch__/…py`; model-class GLOBALs all `__torch__.*` (the non-mangled ones are only `collections.OrderedDict`, `torch.*Storage`, `torch._utils._rebuild_tensor_v2`) | ✓ |
| R4 | `…/extracted/mochi.pth` | state_dict | `state_dict` / `None` | — | — | top level `{state_dict, args, kwargs, optimizer_state_dict, lr_scheduler_state_dict}`; 123 tensors under `state_dict` (`encoder.conv1.weight`, …; `anomaly_score_threshold` is a 0-d tensor); no framework GLOBAL, no `class_embed.*` | ✓ |
| R5 | `…/extracted/checkpoints/resnet18-5c106cde.pth` | legacy_torch | `legacy_torch` / `None` | — | — | tar members `sys_info, pickle, tensors, storages`; GLOBALs `collections.OrderedDict`, `torch.nn.parameter.Parameter` | ✓ |
| R6 | `raw_yolo_world_blue_plate_onnx/model.onnx` | onnx | `onnx` / `yolo` | 1 | `["blue plate"]` | `metadata_props.names` (byte-identical to I3) | ✓ |
| R7a | `raw_blue_plate_yolo_onnx/model.onnx` | onnx | `onnx` / `yolo` | 1 | `[blue_plate]` | `metadata_props.names` | ✓ |
| R7b | `raw_rf_detr_base_coco_onnx/rf-detr-base-coco.onnx` | onnx | `onnx` / `None` | — | — | no `metadata_props`; outputs `pred_boxes [1,300,4]`, `pred_logits [1,300,91]` (ir 8, pytorch 2.8.0) | ✓ |
| R7c | `raw_yolov8n_onnx/yolov8n.onnx` | onnx | `onnx` / `yolo` | 80 | 80 COCO names | `metadata_props.names` (byte-identical to I4) | ✓ |
| **R2′** (added by 1.3, 2026-09-15) | `ref_rfdetr_own_checkpoint/extracted/checkpoint_best_total.pth` (from job `tl13-rfdetr-base-0506`, 127,446,748 B) | rfdetr_checkpoint | `rfdetr_checkpoint` / `rf_detr`, fine_tunable | **1** | **`[blue_plate]`** | `args.class_names` (`args_type: dict`); `num_classes` from `class_embed.bias.shape[0] − 1` (2 − 1); `model_name: RFDETRSmall`; signals `class_embed` ✓, `enc_out_class_embed` groups = 13, 223 `backbone.0.encoder.*` keys, 488 tensors under `model`; zip root is a `tmp…` dir (PTL's atomic save), `data.pkl` + 493 storages, no `constants.pkl`/`code/`. **Top-level layout is a third variant**: `{model, args, model_name, rfdetr_version, state_dict, global_step, epoch, pytorch-lightning_version, loops, callbacks, optimizer_states, lr_schedulers, best_total_source}` — the rfdetr core (`model` + `args` + `model_name`) *plus* the whole PTL `.ckpt` payload (`state_dict`, `optimizer_states`, `lr_schedulers`, `loops`), and **no `model_config`** key (§1.4 expected one) | ✓ |
| — | `ref_lfv_mochi_torchscript/extracted/{compiled.meta, compiled.params, compiled.so, compiled_model.json, dlr.h, libdlr.so, manifest}` | unknown (Neo output, not a checkpoint) | `unknown` ×7 (`compiled.so` / `libdlr.so` flagged `container: elf`) | — | — | — | ✓ |

Wall-clock per file ≤ 0.2 s (R2, 366 MB: 0.18 s; the two `best.pt`: 0.13–0.16 s;
every ONNX ≤ 0.05 s) — only `data.pkl` / the protobuf skeleton is read.

### 2.3 Precision / recall per kind (final module)

| kind | expected | predicted | TP | precision | recall |
|---|---|---|---|---|---|
| `onnx` | 11 | 11 | 11 | 1.00 | 1.00 |
| `ultralytics_checkpoint` | 2 | 2 | 2 | 1.00 | 1.00 |
| `rfdetr_checkpoint` | 1 | 1 | 1 | 1.00 | 1.00 |
| `torchscript` | 1 | 1 | 1 | 1.00 | 1.00 |
| `state_dict` | 1 | 1 | 1 | 1.00 | 1.00 |
| `legacy_torch` | 1 | 1 | 1 | 1.00 | 1.00 |
| `unknown` (negatives) | 7 | 7 | 7 | 1.00 | 1.00 |

24/24 after one fix. `fine_tunable` is `True` for exactly R1, R1′, R2 and
`False` everywhere else; `arch` is `yolo` for every ultralytics-exported ONNX
(detected via `stride`/`task` in `metadata_props`) and for the two `best.pt`,
`rf_detr` for R2, `None` for the RF-DETR ONNX exports (no metadata) and all
non-fine-tunable kinds.

Caveat on the numbers: the positive set has one example each of
`rfdetr_checkpoint`, `torchscript`, `state_dict`, `legacy_torch`, so the
1.00s for those kinds are "no counter-example found", not a measured rate.
R2′ (task 1.3) is the first extra RF-DETR sample and must be re-run through
the probe.

### 2.4 Misclassifications found and fixed

- **First sweep: R1, R1′, R2 and R4 all came back `unknown`** (recall 0/2
  ultralytics, 0/1 rfdetr, 0/1 state_dict; `unknown` precision 7/11) with
  `evidence.pickle_error = "TypeError: APPEND on int"`. Root cause was in the
  pickle stack machine, not in the detection rules:
  `_append(stack[-1], stack.pop())` evaluates `stack[-1]` *before* the pop, so
  the value to be appended was handed in as the container. Every torch
  `data.pkl` hits a single-element `APPEND` (one-item lists inside the
  `_rebuild_tensor_v2` argument tuples), so every non-TorchScript torch zip
  failed the walk and degraded — as designed — to `unknown`. Fixed by popping
  first (`v = stack.pop(); _append(stack[-1], v)`); the second sweep is the
  table above. The garbage-byte behaviour was unaffected either way.
- No remaining misclassifications. Two evidence nits worth knowing, neither
  affecting `kind`: (a) `torchscript_globals_all_mangled` is `False` for R3
  because the GLOBAL set also holds the storage helpers
  (`collections.OrderedDict`, `torch.*Storage`, `_rebuild_tensor_v2`); the
  decisive signal is `constants.pkl` + `code/`, which is what the classifier
  uses. (b) For RF-DETR **ONNX** exports (I7, R7b) `num_classes` is left
  `None`: the `[1, 4+nc, anchors]` heuristic is deliberately YOLO-only and no
  `metadata_props` exist. A `[1, Q, C]` `pred_logits` → `C − 1` rule would
  recover 90 for R7b but is not needed for Requirement 7 (ONNX is not a base
  model) — left out on purpose.

### 2.5 What is recoverable from an RF-DETR `.pth`

- **Published `rf-detr-nano.pth` (R2)** — older `{model, optimizer,
  lr_scheduler, epoch, args(Namespace)}` layout. Recoverable by inspection:
  `num_classes = 90` from `class_embed.bias` (91 − 1, the COCO head), the
  architecture knobs (`encoder`, `resolution`, `num_queries`, `group_detr`,
  `dec_layers`, `hidden_dim`) from `args`, and `epoch`. **Not recoverable:
  `class_names`** — `args.class_names` is absent; COCO names come from the
  `rfdetr` package (`rfdetr/util/coco_classes.py`), so a portal that wants to
  pre-fill names for a *published* base must carry the COCO list itself (or
  show only the count). `args.num_classes = 2` is present and wrong; the
  probe ignores it, as `load_pretrain_weights` does.
- **Trained `checkpoint_best_total.pth` (R2′, expected)** — 1.10.1
  `BestModelCallback` layout `{model, args(dict), epoch, model_name,
  model_config, callbacks[, ema_model]}`. Per the package source (§1.4) `args`
  is a plain dict with **`class_names` back-filled from the datamodule** and
  `model_config.num_classes` synced from `class_embed.weight`. The probe
  already reads both: `args.class_names` → `class_names` (`names_source =
  args.class_names`), `class_embed.bias` → `num_classes`, plus
  `model_name` / `model_config.num_classes` into `evidence`. So for our own
  fine-tuned RF-DETR both the count **and the names** should be recoverable
  from the file alone — **confirmed on the real R2′ by task 1.3
  (2026-09-15, §2.2 last row)**: `num_classes = 1`, `class_names =
  ['blue_plate']` from `args.class_names`, `model_name = 'RFDETRSmall'`,
  unchanged probe code. One correction to the expectation above: the real
  1.10.1 `checkpoint_best_total.pth` carries **no `model_config`** key and
  *does* carry the full PTL `.ckpt` payload beside the rfdetr core
  (`state_dict`, `optimizer_states`, `lr_schedulers`, `loops`,
  `global_step`, `pytorch-lightning_version`, `rfdetr_version`,
  `best_total_source`), so it is 127 MB rather than a stripped weights-only
  file. The probe keys on `model` + `args` + the `class_embed.*` names, so
  the extra keys are inert; `train_rfdetr.checkpoint_num_classes` reads the
  head shape first and never needs `model_config`. Both rfdetr layouts seen
  so far (published `{model, optimizer, lr_scheduler, epoch, args(Namespace)}`
  and this one) classify correctly.
- For comparison, the ultralytics `best.pt` recovers everything (`names`,
  `nc`, base checkpoint, `imgsz`) today, and every ultralytics ONNX export
  carries `names` in `metadata_props`; only the RF-DETR ONNX exports and the
  published RF-DETR `.pth` are name-less.

## 3. Fine-tune runs (task 1.3)

Status: **part 1 done — 2026-09-15 (a), (b1)**; **part 2 done — 2026-09-15
(b2), (c-yolo), (c-rfdetr)**. Seven SageMaker jobs in total (§3.2; the
(c-rfdetr) test needed two attempts), all `Completed`; R2′
(`checkpoint_best_total.pth`) exists in S3 and was itself
used as a base in (b2) and (c-rfdetr). Two entry-point findings came out of
the 1 → 2-class runs: a cosmetic hard-coded "expect 5" in `train.py`'s
export log, and a **real** one in `train_rfdetr.py` — on the base-weights
path rfdetr 1.10.1 kept the checkpoint's 1-class head for a 2-class manifest
and our contract check let it through; fixed by pinning `num_classes` (see
(c-rfdetr) in §3.3) and verified by the re-run. §3.4 answers Req 5.4(a) for
both arches; §3.6 is the baseline-vs-fine-tune table.

### 3.1 Launch recipe actually used

Manual launch path (README "Launching"), reusing exactly what the portal job
`blue-plate-trained-imts-20260913-142306` (record `d62a831d…`) used:

| item | value |
|---|---|
| role | `arn:aws:iam::164152369890:role/DDASageMakerExecutionRole` |
| image | `763104351884.dkr.ecr.us-east-1.amazonaws.com/pytorch-training:2.5.1-gpu-py311-cu124-ubuntu22.04-sagemaker` |
| instance | `ml.g4dn.xlarge` ×1, 60 GB volume, `MaxRuntimeInSeconds=5400`, `EnableNetworkIsolation=false` |
| manifest | `s3://ryvan-cookies/labeled/labeling-9cbcdb4c/output.manifest` (145 images / 390 boxes, 1 class `blue_plate`; no `IMAGES_S3` → images from `source-ref`) |
| YOLO base (R1′) | `s3://ryvan-cookies/models/training/blue-plate-trained-imts-20260913-142306/blue-plate-trained-imts-20260913-142306/output/model.tar.gz`, member `best.pt` |
| spike prefix | `s3://ryvan-cookies/spike/tl-1-3/<job>/sourcedir.tar.gz` (bundle per job) and `…/<job>/output/<job>/output/model.tar.gz` (artifact) |
| request shape | `HyperParameters = {sagemaker_program, sagemaker_submit_directory, **env}` **and** `Environment = env` (the portal's duplication; the entry points read bare env names), plus the portal's four `MetricDefinitions` regexes so `FinalMetricDataList` carries `test:mAP50` etc. |
| logs | CloudWatch group `/aws/sagemaker/TrainingJobs`, stream prefix `<job>/algo-1-…` |

Sourcedirs were built with `datasets/detection_training/build_sourcedir.sh
--arch yolo|rf_detr` from the working tree at launch time (five-file bundles:
entry point, `requirements.txt`, `_common.py`, the two converter files).

Reference baselines (published-checkpoint starts, same manifest): YOLO
`yolo11s.pt` → `blue-plate-trained-imts-20260913-142306`: **test mAP@50
0.995, mAP@50-95 0.919**, precision 0.9989, recall 1.0, 1207 s
training/billable, 100 epochs / patience 30 (the manual-launch twin
`blue-plate-yolo-20260912-231818` is identical to three decimals, 1227 s).
There is no RF-DETR baseline yet — (b1) below is it.

### 3.2 Run ledger

| # | job | arch | base | env (beyond `MANIFEST_S3`) | launched (UTC) | status | `TEST METRICS` | train / billable s | artifact |
|---|---|---|---|---|---|---|---|---|---|
| (a) | `tl13-yolo-ft-0453` | yolo | R1′ `best.pt` (1-class, same manifest) | `IMGSZ=1280 EPOCHS=30 BATCH=4 PATIENCE=10 ONNX_OPSET=17 BASE_WEIGHTS=yolo11s.pt BASE_WEIGHTS_S3=<R1′ tarball> BASE_WEIGHTS_MEMBER=best.pt` | 2026-09-15 04:53:45 | **Completed** 05:05:26 | `{"test_map50": 0.995, "test_map50_95": 0.9218, "test_precision": 0.9990, "test_recall": 1.0}` | **592 / 592** (early-stopped at epoch 30, best epoch 20; 0.074 h of pure training) | `s3://ryvan-cookies/spike/tl-1-3/tl13-yolo-ft-0453/output/tl13-yolo-ft-0453/output/model.tar.gz` (`model.onnx` 38,411,162 B `[1,5,33600]`, `best.pt`, `training_metadata.json` with `base_weights` = the R1′ URI, `base_weights_member: best.pt`) |
| (b1) | `tl13-rfdetr-base-0506` | rf_detr | published `rf-detr-small.pth` (COCO, 90-class) | `RFDETR_SIZE=small RESOLUTION=512 EPOCHS=30 BATCH=4 GRAD_ACCUM=4 LR=0.0001 PATIENCE=10 ONNX_OPSET=17` | 2026-09-15 05:06:24 | **Completed** 05:23:26 | `{"test_map50": 1.0, "test_map50_95": 0.9528, "test_precision": 1.0, "test_recall": 1.0}` | **767 / 767** (early-stopped at epoch 15, best epoch 5) | `s3://ryvan-cookies/spike/tl-1-3/tl13-rfdetr-base-0506/output/tl13-rfdetr-base-0506/output/model.tar.gz` (`model.onnx` 114,247,641 B `input [1,3,512,512]` → `dets [1,300,4]`, `labels [1,300,2]`; `checkpoint_best_total.pth` 127,446,748 B = **R2′**; `training_metadata.json` with `detection_arch: rf_detr`) |
| (b2) | `tl13-rfdetr-ft-0533` | rf_detr | (b1)'s `checkpoint_best_total.pth` (1-class, same manifest) = R2′ | as (b1) + `BASE_WEIGHTS_S3=<(b1) artifact> BASE_WEIGHTS_MEMBER=checkpoint_best_total.pth` | 2026-09-15 05:33:54 | **Completed** 05:50:46 | `{"test_map50": 1.0, "test_map50_95": 0.9610, "test_precision": 1.0, "test_recall": 1.0}` | **717 / 717** (instance 05:38:49 → 05:50:46; early-stopped after epoch 2's best, ≈13 epochs ≈ 6¼ min of training) | `s3://ryvan-cookies/spike/tl-1-3/tl13-rfdetr-ft-0533/output/tl13-rfdetr-ft-0533/output/model.tar.gz` (`model.onnx` 114,247,641 B `dets [1,300,4]` / `labels [1,300,2]`; `checkpoint_best_total.pth` 127,446,748 B; `training_metadata.json` with `base_weights` = the (b1) URI, `base_weights_member: checkpoint_best_total.pth`, `class_names: [blue_plate]`) |
| (c-yolo) | `tl13-yolo-nc2-0553` | yolo | R1′ `best.pt` (1-class) onto the **2-class** manifest `spike/tl-1-3/manifests/blue-plate-2class.manifest` | as (a) with that `MANIFEST_S3`, `EPOCHS=3 PATIENCE=3` | 2026-09-15 05:53:34 | **Completed** 06:02:26 | `{"test_map50": 0.3064, "test_map50_95": 0.1842, "test_precision": 0.5160, "test_recall": 0.5}` (meaningless by construction — see §3.3) | **426 / 426** (instance 05:55:20 → 06:02:26; `3 epochs completed in 0.011 hours`) | `s3://ryvan-cookies/spike/tl-1-3/tl13-yolo-nc2-0553/output/tl13-yolo-nc2-0553/output/model.tar.gz` (`model.onnx` 38,412,729 B **`[1,6,33600]`**, `best.pt` 19,296,147 B, `training_metadata.json` with `onnx_output_shape [1,6,33600]`) |
| (c-rfdetr) ① | `tl13-rfdetr-nc2-0603` | rf_detr | (b1) checkpoint (1-class) onto the same **2-class** manifest | as (b2) with that `MANIFEST_S3`, `EPOCHS=3 PATIENCE=3` | 2026-09-15 06:03:16 | **Completed** 06:16:14 — **but the head was NOT widened** (entry-point bug, §3.3) | `{"test_map50": 0.5997, "test_map50_95": 0.5665, "test_precision": 0.5041, "test_recall": 1.0}` (1-class head scored on 2-class labels) | **446 / 446** (instance 06:08:48 → 06:16:14) | `s3://ryvan-cookies/spike/tl-1-3/tl13-rfdetr-nc2-0603/output/tl13-rfdetr-nc2-0603/output/model.tar.gz` — **mislabelled**: `labels [1,300,2]` (1 class + background) but `training_metadata.json` says `num_classes: 2, logits_slots: 2, background_slot: null`; kept as evidence, not a usable base |
| (c-rfdetr) ② | `tl13-rfdetr-nc2b-0625` | rf_detr | same, after the `build_model(num_classes=)` fix | identical env; sourcedir rebuilt from the fixed tree | 2026-09-15 06:25:49 | **Completed** 06:34:35 | `{"test_map50": 0.5680, "test_map50_95": 0.5389, "test_precision": 0.5013, "test_recall": 0.98}` (meaningless by construction) | **456 / 456** (instance 06:26:59 → 06:34:35; 3 epochs ≈ 90 s) | `s3://ryvan-cookies/spike/tl-1-3/tl13-rfdetr-nc2b-0625/output/tl13-rfdetr-nc2b-0625/output/model.tar.gz` (`model.onnx` 114,249,697 B **`labels [1,300,3]`**; `checkpoint_best_total.pth` 127,461,084 B — 14,336 B more than the 1-class file, the widened `class_embed` + 13 `enc_out_class_embed` rows; `training_metadata.json` `num_classes: 2, class_names: [blue_plate, blue_plate_b], logits_slots: 3, background_slot: 2`) |

### 3.3 Observations per run

**(a) `tl13-yolo-ft-0453`** — CloudWatch stream
`tl13-yolo-ft-0453/algo-1-1789448134`. Timeline: created 04:53:45, instance
+ image download until 04:59:22 (the 10 GB DLC pull is ~4½ min of the
billable time), `pip install` of the YOLO pins 5 s (04:59:23→04:59:28;
`ultralytics 8.3.40`, `onnx 1.17.0`, `onnxruntime 1.19.2`, `onnxslim 0.1.34`
— torch 2.5.1+cu124 / numpy 1.26.4 already in the image), 145 images
downloaded from `source-ref`, converter split 101 train / 22 val / (22 test).
Base-weights path worked first time:
`base weights: downloading …/model.tar.gz (member best.pt)` →
`…/base_weights/best.pt (19307731 bytes)`; `_log_class_names` printed
`base checkpoint classes match the manifest: ['blue_plate']`; ultralytics
then logged `Transferred 499/499 items from pretrained weights` (i.e. **no
head re-init** — identical `nc`), `Detect [1, [128, 256, 512]]`, AdamW
lr=0.002 auto, 26 iterations/epoch at ~2.9 it/s on the T4 (≈10 s/epoch +
val). `EarlyStopping: … no improvement observed in last 10 epochs. Best
results observed at epoch 20`; `30 epochs completed in 0.074 hours`; test
split: `TEST METRICS: {"test_map50": 0.995, "test_map50_95":
0.9218316165903062, "test_precision": 0.9990150232189816, "test_recall":
1.0}`; export `(1, 3, 1280, 1280) → (1, 5, 33600)`, opset 17, onnxslim,
`channel dim = 5`; `DONE`; `Reporting training SUCCESS` at 05:05:04.
`FinalMetricDataList` carries all four metrics (the portal regexes match).

  - **Result vs baseline**: fine-tuning the already-converged 1-class
    `best.pt` for 30 more epochs gives **mAP@50 0.995 (= baseline), mAP@50-95
    0.9218 vs 0.9192 (+0.003)** in **592 s vs 1207 s** — i.e. half the
    wall-clock for the same quality, with ~4½ min of that being fixed
    instance/image overhead (pure training was 0.074 h ≈ 4.4 min vs the
    baseline's 100 epochs). The manifest is the same one the base was
    trained on, so this measures "warm start on the same data", not
    "new data on an old model"; the class-set-change case is row (c-yolo).
  - **Side effect worth knowing**: during `model.export(format="onnx")`
    ultralytics' `check_requirements` **AutoUpdate**-installed
    `onnxruntime-gpu 1.30.0` (246.7 MB, 4.7 s) beside our pinned
    `onnxruntime 1.19.2` (a CUDA box makes it prefer the GPU package). The
    export still succeeded with `onnxruntime 1.19.2` in-process ("Restart
    runtime … for updates to take effect"), so it is harmless here, but it
    is 250 MB of network per job and would break under
    `EnableNetworkIsolation=true`. Not fixed in this pass (pre-existing
    `train.py` behaviour, unrelated to base weights). Ultralytics 8.3.40
    gates this on its `autoinstall` *setting*
    (`from ultralytics import settings; settings.update(autoinstall=False)`
    before `export`), not an env var — a one-liner for 1.4 to consider.
  - No entry-point fix was needed for (a): `fetch_base_weights` (tarball +
    member), `_log_class_names` and the metadata path all worked on the
    first run.

**(b1) `tl13-rfdetr-base-0506`** — first real execution of
`train_rfdetr.py`. CloudWatch stream `tl13-rfdetr-base-0506/algo-1-1789449038`.
Created 05:06:24, waited for capacity until ~05:10 (the YOLO instance had
just been released), image pulled by 05:14:33. **`pip install` of
`requirements-rfdetr.txt` took 23 s** (05:14:33 → 05:14:56) and resolved
cleanly on the `pytorch-training:2.5.1-gpu-py311-cu124` DLC without touching
torch/torchvision; what landed: `rfdetr 1.10.1`, `transformers 5.17.0`,
`pytorch_lightning 2.6.6`, `torchmetrics 1.8.2`, `faster-coco-eval 1.8.0`,
`pycocotools 2.0.11`, `torch-hungarian 0.1.0rc0` (pure Python; Triton path
is skipped on a T4 so it falls back to SciPy), `peft 0.20.0`,
`supervision 0.30.3`, `roboflow 1.4.2`, `huggingface-hub 1.31.0`,
`onnx 1.17.0`, `onnxsim 0.7.3`, `onnx_graphsurgeon 0.6.1`,
`onnxruntime 1.19.2`, `polygraphy 0.53.4`, `numpy` left at 1.26.4. The
entry point started, downloaded 145 images from `source-ref`, and invoked
the converter with `--format coco --coco-layout rfdetr` (`classes (1):
['blue_plate']`, `assert_splits` passed). Then, all first-try:

- `RFDETRSmall()` → `Downloading pretrained weights for
  /root/.roboflow/models/rf-detr-small.pth` (05:15:46 → 05:15:52, 6 s), two
  expected rfdetr warnings about DINOv2 PE count / patch size 16 ("not a
  problem if finetuning a pretrained RF-DETR model").
- `.train(**kwargs)` with exactly the kwargs `train_kwargs()` builds
  (`dataset_dir, epochs, batch_size, grad_accum_steps, lr, output_dir,
  resolution, early_stopping, early_stopping_patience, run_test,
  tensorboard, wandb, class_names`) was **accepted** — `TrainConfig`'s
  `extra="forbid"` did not reject anything, so the `# PIN-IN-1.3` guess for
  `.train()` is confirmed as-is.
- **Head re-init observed in the wild** (this *is* the Req 5.4(a) answer
  for RF-DETR, 90 → 1): rfdetr logged `Checkpoint has 90 classes but model
  is configured for 1. The detection head will be re-initialized to 1
  classes.` and training proceeded. `_align_num_classes_from_dataset` set
  `num_classes=1` from `train/_annotations.coco.json`, `load_pretrain_weights`
  did the resize.
- Runtime warnings worth knowing (none fatal): rfdetr 1.10.1 changed its
  resize path (`antialias=True`; "mAP may drift on existing benchmarks");
  PTL's `log_every_n_steps` nudge (26 steps/epoch); `torch_linear_assignment`
  falls back to SciPy on the T4 (compute capability 7.5 < 8.0) — a
  per-step CPU Hungarian solve, fine at this dataset size.
- Speed/quality: first epoch done in ~25 s (05:15:58 → 05:16:24) including
  validation; **epoch-1 EMA val: mAP@50-95 0.8037, mAP@50 0.9625, F1 0.911**
  — the COCO-pretrained DETR adapts to the single class almost
  immediately. Steady state ≈ **29 s/epoch** (26 optimizer steps of
  `BATCH=4 × GRAD_ACCUM=4` + EMA validation), so 30 epochs ≈ 14½ min of
  training on top of ~8 min fixed overhead (capacity wait, image pull,
  pip, weights). EMA val trajectory: epoch 1 0.8037 → 2 0.8183 → 3 0.9178
  → 4 0.9261 → 6 **0.9265 mAP@50-95 with mAP@50 = 1.000, F1 1.000**
  (`Best EMA metric improved …` lines); by epoch 6 it already matches the
  YOLO run's *test* mAP@50-95 (0.92) on the *val* split.
- **Early stopping** fired at epoch 15 (no improvement for 10 epochs after
  epoch 5); rfdetr's own `run_test=True` pass printed a `Test (Epoch 15/30)`
  table, then our `train_model` found
  `best checkpoint: /opt/ml/input/work/out/checkpoint_best_total.pth
  (127446748 bytes)` — the `BestModelCallback` file name is confirmed.
- **`reload_best` → `RFDETRSmall.from_checkpoint(best, trust_checkpoint=True)`
  worked** (no `WARN: … from_checkpoint failed` fallback line), and
  **`.evaluate(split="test", dataset_dir=…, batch_size=4, output_dir=…,
  tensorboard=False)` returned exactly the keys `metrics_from_eval` expects**:
  `{"test/loss": 2.88, "test/mAP_50_95": 0.9528, "test/mAP_50": 1.0,
  "test/mAP_75": 1.0, "test/mAR": 0.982, "test/F1": 1.0, "test/precision":
  1.0, "test/recall": 1.0}` → `TEST METRICS: {"test_map50": 1.0,
  "test_map50_95": 0.9528191685676575, "test_precision": 1.0, "test_recall":
  1.0}`; `FinalMetricDataList` carries all four (portal regexes match).
  (The `Test (Epoch 1/100)` banner printed by the eval-only trainer is
  cosmetic — its config defaults to 100 epochs.)
- **`.export(output_dir, opset_version=17, batch_size=1, dynamic_batch=False,
  format="onnx", verbose=False, output_name="model")` worked** and produced
  `<export_dir>/model.onnx` as the `_naming.py` reading predicted
  (`Successfully exported ONNX model to: /opt/ml/input/work/export/model.onnx`,
  05:22:56; a handful of tracer `TracerWarning`s about tensor-shape
  Python bools, harmless). Copied to `/opt/ml/model/model.onnx`
  (114,247,641 B). **Contract check passed**: `ONNX inputs : [('input',
  [1, 3, 512, 512])]`, `ONNX outputs: [('dets', [1, 300, 4]), ('labels',
  [1, 300, 2])]`, `ONNX contract OK: boxes [1, 300, 4] logits [1, 300, 2]
  (Q=300, num_classes=1, background slot=yes)` — i.e. the `C+1` trailing
  background slot the device postprocessor must ignore is real.
- `kept … checkpoint_best_total.pth -> /opt/ml/model/checkpoint_best_total.pth`,
  `DONE`, `Reporting training SUCCESS` 05:23:00. Training/billable **767 s**
  (05:10:39 → 05:23:26), of which ~4 min instance/image, 23 s pip, 6 s
  weights, ~7 min training (15 epochs), ~1½ min eval + export.
- **Result vs YOLO on the same manifest/test split**: RF-DETR small @512
  from COCO weights reaches **mAP@50 1.000 / mAP@50-95 0.9528** vs YOLO
  (fine-tuned) 0.995 / 0.9218 and YOLO baseline 0.995 / 0.9192 — better
  localisation at a quarter of the pixel count (512² vs 1280²), in 767 s
  vs 1207 s. Caveat as always: 22 test images from one capture session.
- **No entry-point fix was needed for (b1)** either: every `PIN-IN-1.3`
  guess in `train_rfdetr.py` held on the first run. The module docstring's
  pin block now records that they were confirmed on this job.

**(b2) `tl13-rfdetr-ft-0533`** — RF-DETR fine-tuned from its *own* 1-class
checkpoint (R2′, the 1.10.1 `BestModelCallback` layout with `args` as a dict
carrying `class_names`). CloudWatch stream `tl13-rfdetr-ft-0533/algo-1-1789450729`
(709 lines). Created 05:33:54, ~5 min capacity wait (the (a)/(b1) instance
was still being released), image pulled by 05:42:31, pip 23 s (same
resolution as (b1): `rfdetr 1.10.1`, `pytorch_lightning 2.6.6`, …), 145
images from `source-ref`, converter `classes (1): ['blue_plate']`. Then:

- `fetch_base_weights` on a 224 MB tarball worked first time: `base weights:
  downloading …/tl13-rfdetr-base-0506/…/model.tar.gz (member
  checkpoint_best_total.pth)` → `…/base_weights/checkpoint_best_total.pth
  (127446748 bytes)`.
- **Our `log_checkpoint_classes` read the 1.10.1 layout correctly**:
  `base checkpoint classes match the manifest: ['blue_plate']` (it found
  `args.class_names` in R2′ — the published-weights case has no names, so
  this is the first confirmation that our own checkpoints round-trip them).
- `RFDETRSmall(pretrain_weights=<R2′>, trust_checkpoint=True)` logged
  `Checkpoint has 1 classes but model is configured for 90. Using checkpoint
  class count (1). Pass num_classes=1 to suppress this warning.` — that is
  the **constructor** (default `num_classes=90`) deferring to the checkpoint
  because we did not pin `num_classes`; **not** a re-init. `.train()` then
  aligned `num_classes=1` from `train/_annotations.coco.json`, which matched
  the head, so **no `will be re-initialized` line appeared anywhere in the
  log** — the expected no-re-init case (see §3.4).
- Training: `Best EMA metric improved to 0.9224 (epoch 0)` — i.e. the warm
  start begins where (b1) ended (its val mAP@50-95 was 0.9265 at best) —
  `0.9392 (epoch 2)`, then no further improvement; early stopping ended the
  run ≈13 epochs in (test-split build at 05:50:09, ≈6¼ min of training at
  ≈29 s/epoch). `reload_best` → `from_checkpoint` worked again; rfdetr
  `evaluate(test)`: `{"test/loss": 3.13, "test/mAP_50_95": 0.9610,
  "test/mAP_50": 1.0, "test/mAP_75": 1.0, "test/mAR": 0.973, "test/F1": 1.0,
  …}` → `TEST METRICS: {"test_map50": 1.0, "test_map50_95":
  0.9610421657562256, "test_precision": 1.0, "test_recall": 1.0}`.
- Export identical to (b1): `input [1,3,512,512]` → `dets [1,300,4]`,
  `labels [1,300,2]`, `ONNX contract OK … (Q=300, num_classes=1, background
  slot=yes)`, `model.onnx` 114,247,641 B; `kept … checkpoint_best_total.pth`;
  `DONE`; `Reporting training SUCCESS` 05:50:23. Training/billable **717 s**
  (05:38:49 → 05:50:46).
- **Result vs (b1)**: mAP@50 1.000 (=), **mAP@50-95 0.9610 vs 0.9528
  (+0.008)** in 717 s vs 767 s — the same "warm start on the same data"
  caveat as (a) applies, but the checkpoint → checkpoint path (download,
  class-name check, load with `trust_checkpoint`, train, re-export, re-save
  `checkpoint_best_total.pth`) is now proven end-to-end, and the produced
  artifact is itself a valid base for a further round (same layout as R2′).
- No entry-point fix needed.

**(c-yolo) `tl13-yolo-nc2-0553`** — the **1 → 2-class head test** for
ultralytics. Same recipe as (a) but `MANIFEST_S3` = the synthetic 2-class
manifest (`blue-plate-2class.manifest`: 145 lines, every odd line's boxes
relabelled `class_id 1`, class-map `{0: blue_plate, 1: blue_plate_b}` → 196 /
194 boxes; **same images, same boxes**, so the two classes are visually
indistinguishable and any metric is noise — only the head behaviour is
under test), `EPOCHS=3 PATIENCE=3`. CloudWatch stream
`tl13-yolo-nc2-0553/algo-1-1789451720` (595 lines). Created 05:53:34,
instance 05:55:20, training 05:59:58, done 06:02:26 (426 s; only ~2 min of
that is the entry point). Evidence, in log order:

- Converter: `classes : ['blue_plate', 'blue_plate_b']`; `data.yaml`
  `names: {0: blue_plate, 1: blue_plate_b}`.
- `fetch_base_weights` → `…/base_weights/best.pt (19307731 bytes)` (R1′).
- **Our `_log_class_names` line** (the Req 7.4 old-vs-new log):
  ```
  base checkpoint classes (1): ['blue_plate']
  manifest classes (2): ['blue_plate', 'blue_plate_b']
  class set differs: the detection head will be re-initialised
  ```
- **ultralytics 8.3.40 re-initialised the head itself, as predicted**:
  `Overriding model.yaml nc=1 with nc=2`, the model table's last row
  `23 [16, 19, 22] 1 820182 ultralytics.nn.modules.head.Detect [2, [128,
  256, 512]]` (the `Detect` head rebuilt for 2 classes; `YOLO11s summary:
  319 layers, 9,428,566 parameters`), then **`Transferred 493/499 items from
  pretrained weights`** — 6 tensors *not* transferred (vs `499/499` in (a)
  where `nc` matched); the count matches the three per-level class-score
  `Conv2d` layers (weight + bias) at the end of `Detect.cv3`, whose
  out-channels changed from 1 to 2 (ultralytics' `intersect_dicts` drops
  shape-mismatched tensors and leaves them at their fresh init). Everything
  else (backbone, neck, box branch `cv2`, DFL) carried over.
- Training ran 3 epochs (`3 epochs completed in 0.011 hours`); val per class
  after epoch 3: `blue_plate P 0.023 R 1.0`, `blue_plate_b P 1.0 R 0.0` — the
  network puts every box in one class, which is the correct answer for
  indistinguishable classes and confirms the metrics are meaningless here.
  `TEST METRICS: {"test_map50": 0.3064, "test_map50_95": 0.1842,
  "test_precision": 0.5160, "test_recall": 0.5}`.
- **Export shape changed with the head**: ultralytics `output shape(s) (1, 6,
  33600)`, our `ONNX outputs: [('output0', [1, 6, 33600])]`,
  `training_metadata.json` → `"onnx_output_shape": [1, 6, 33600]` (4 + 2).
  The exported `best.pt` (19,296,147 B) is a 2-class ultralytics checkpoint.
- **One cosmetic entry-point bug surfaced**: `train.py::export` printed
  `channel dim = 6 (expect 4 + num_classes = 5)` — the "expected" value was
  a hard-coded `5` from the single-class era. Fixed in this pass: `export()`
  now takes the manifest's class list and prints `expect 4 + num_classes =
  <4+nc>`; no behaviour change (log line only), not re-run on SageMaker.
  Also worth noting for 1.4: the YOLO `training_metadata.json` still carries
  **no `num_classes`/`class_names`** (only the RF-DETR one does), so for a
  YOLO job the checkpoint's `names` remain the only source of class names.
- The `onnxruntime-gpu 1.30.0` AutoUpdate side effect from (a) recurred
  (4.8 s, 246.7 MB); still harmless.

**(c-rfdetr) `tl13-rfdetr-nc2-0603`** — the **1 → 2-class head test** for
rfdetr, first attempt: (b2)'s recipe on the 2-class manifest, `EPOCHS=3
PATIENCE=3`. CloudWatch stream `tl13-rfdetr-nc2-0603/algo-1-1789452528`
(634 lines). Created 06:03:16, ~5 min capacity wait, instance 06:08:48,
training 06:13:26, `Reporting training SUCCESS` 06:15:50, `Completed`
06:16:14 (**446 s**). The job *succeeded* — and that is the problem: **it
did not re-initialise the head.** Evidence, in log order:

- Converter `classes (2): ['blue_plate', 'blue_plate_b']`; base weights
  fetched (R2′, 127,446,748 B); **our `log_checkpoint_classes` line was
  right**: `base checkpoint classes: ['blue_plate']` / `manifest classes
  (2): ['blue_plate', 'blue_plate_b']` / `class set differs: rfdetr will
  re-initialise the detection head`.
- Constructor (`RFDETRSmall(pretrain_weights=<R2′>, trust_checkpoint=True)`):
  `Checkpoint has 1 classes but model is configured for 90. Using checkpoint
  class count (1). Pass num_classes=1 to suppress this warning.` — as in
  (b2), the default `num_classes=90` deferred to the checkpoint's 1.
- `.train(..., class_names=['blue_plate', 'blue_plate_b'])`, then the line
  that shows the failure: **`Dataset '/opt/ml/input/work/dataset' has 2
  classes but model was initialized with num_classes=1. Using the model's
  configured value (1). If this is unintentional, reinitialize the model with
  num_classes=2.`** No `will be re-initialized` line anywhere. Training ran 3
  epochs on a **1-class head** (`Best EMA metric improved to 0.4898 (epoch
  0)` → `0.5234 (epoch 1)`; `Trainer.fit stopped: max_epochs=3 reached`),
  `TEST METRICS: {"test_map50": 0.5997, "test_map50_95": 0.5665,
  "test_precision": 0.5041, "test_recall": 1.0}` (half the boxes carry a
  label the head cannot emit, hence precision ≈ 0.5 / recall 1.0 on the
  class it *can* emit).
- Export: `labels [1, 300, 2]` — the **same shape as the 1-class runs**
  (1 class + background), where a 2-class head would be `[1, 300, 3]`. Our
  `verify_two_outputs` accepted it because it allows `num_classes` **or**
  `num_classes + 1` slots and printed `ONNX contract OK: boxes [1, 300, 4]
  logits [1, 300, 2] (Q=300, num_classes=2, background slot=no)`; the
  artifact's `training_metadata.json` therefore claims `num_classes: 2,
  class_names: [blue_plate, blue_plate_b], logits_slots: 2, background_slot:
  null` for a model whose slot 1 is really the background column. On a
  device this would silently label every background-max query
  `blue_plate_b` — exactly the kind of mismatch Req 7.4 exists to prevent.
- **Root cause (rfdetr 1.10.1 source, `models/weights.py` +
  `detr.py::_align_num_classes_from_dataset`)**: with `pretrain_weights`
  set and `num_classes` *not* passed, `load_pretrain_weights` auto-aligns
  `mc.num_classes = checkpoint_num_classes - 1` — and that assignment adds
  `num_classes` to Pydantic's `model_fields_set`. `.train()` then treats the
  field as **user-pinned** (`user_overrode = "num_classes" in
  model_fields_set`) and refuses to widen it to the dataset's count. Only
  `from_checkpoint()` clears the flag for checkpoint-derived fields; the
  plain constructor path our `build_model` used does not. In (b1) this never
  showed because the constructor's *default* 90 is not in `model_fields_set`
  (so `.train()` aligned 90 → 1 and the Lightning reload re-initialised the
  head), and in (b2) the checkpoint already matched the dataset.
- **Entry-point fix (this pass)**: `train_rfdetr.py::build_model` now
  passes `num_classes=len(manifest class_names)` on the base-weights path
  (the remedy rfdetr's own warning prescribes). With the field genuinely
  user-set, `load_pretrain_weights` keeps the configured count, resizes the
  head to the checkpoint's width only to load it, then
  `reinitialize_detection_head(configured + 1)` (`weights.py` ≈ l. 635); the
  published-weights path is unchanged (its behaviour was proven in (b1)).
  Static tests added in `tests/test_train_rfdetr_static.py`
  (`test_build_model_pins_num_classes_on_base_weights_path`,
  `test_build_model_published_weights_passes_no_kwargs`). Re-run as
  `tl13-rfdetr-nc2b-<HHMM>` below.
- Also worth carrying to 1.4: `verify_two_outputs`'s "`C` or `C+1`"
  leniency masked this. rfdetr 1.10.1 always emits `C+1`; tightening the
  check to `C+1` (or at least FATAL-ing when `logits_slots == C` **and** the
  base checkpoint's head was narrower than `C+1`) would have failed this job
  instead of shipping a mislabelled artifact.

**(c-rfdetr) ② `tl13-rfdetr-nc2b-0625`** — the re-run with the fix, same
env, sourcedir rebuilt from the fixed tree by the same launcher. CloudWatch
stream `tl13-rfdetr-nc2b-0625/algo-1-1789453618` (634 lines). Created
06:25:49, instance 06:26:59 (no capacity wait this time), training 06:31:53,
`Reporting training SUCCESS` 06:34:09, `Completed` 06:34:35 (**456 s**).
What changed in the log, in order:

- Same converter / base-weights / `log_checkpoint_classes` lines as ①.
- Constructor warning now reads **`Checkpoint has 1 classes but model is
  configured for 2. Using checkpoint class count (1). Pass num_classes=1 to
  suppress this warning.`** — "configured for 2" proves the pin reached
  `model_config`; the "Using checkpoint class count" wording is rfdetr's
  fixed text for the *checkpoint-narrower* case and is misleading here: with
  `num_classes` user-set, `load_pretrain_weights` does **not** adopt the
  checkpoint's count — it resizes the head to 2 slots to load the weights
  and then `reinitialize_detection_head(3)` (`weights.py` ≈ l. 635). The
  same line repeats once more when the Lightning module reloads the
  weights inside `.train()`.
- **The `Dataset … has 2 classes but model was initialized with
  num_classes=1` line is gone** — `_align_num_classes_from_dataset` found
  dataset 2 == configured 2 and returned silently.
- Training: `Best EMA metric improved to 0.5186 (epoch 0)` → `0.5304
  (epoch 1)` → `0.5477 (epoch 2)`, `Trainer.fit stopped: max_epochs=3
  reached`. **`best checkpoint: … checkpoint_best_total.pth (127461084
  bytes)`** vs 127,446,748 B in every 1-class run — the extra 14,336 B are
  the widened head (one more `class_embed` row of 256 + bias, and the same
  for each of the 13 `enc_out_class_embed` group heads: 14 × (256 + 1) × 4 B
  = 14,392 B ≈ the delta once pickle framing is counted).
- `TEST METRICS: {"test_map50": 0.5680, "test_map50_95": 0.5389,
  "test_precision": 0.5013, "test_recall": 0.98}` — still meaningless (the
  two classes are the same objects), but note recall dropped from ① 's 1.0
  because the head can now *choose* the wrong class.
- **Export**: `ONNX outputs: [('dets', [1, 300, 4]), ('labels', [1, 300,
  3])]`, `ONNX contract OK: boxes [1, 300, 4] logits [1, 300, 3] (Q=300,
  num_classes=2, background slot=yes)`; `training_metadata.json` now says
  `num_classes: 2, class_names: [blue_plate, blue_plate_b], logits_slots: 3,
  background_slot: 2` — internally consistent, and the shape the task text
  asked to confirm (`labels [1,300,3]`).
- So the RF-DETR 1 → 2 answer is: **rfdetr re-initialises the head itself
  once `num_classes` is pinned to the manifest's count**; without the pin it
  silently keeps the old head. Our entry point's only job is the pin plus
  the class-name log line.

### 3.4 Class-head behaviour when `nc` differs (Req 5.4(a))

**Answer: both trainers re-initialise the class head automatically when
the manifest's class count differs from the checkpoint's, and both leave it
alone when it matches. Our entry points do not have to touch the head; they
only need to make the change visible (which they now do) and, for Req 7.4,
to pass the manifest's class list so the new head is labelled.** Observed
in all four combinations:

| arch | case | run | what the trainer logged | head | our log line |
|---|---|---|---|---|---|
| YOLO (ultralytics 8.3.40) | 1 → 1 (same set) | (a) | `Transferred 499/499 items from pretrained weights`; no `Overriding` line | kept | `base checkpoint classes match the manifest: ['blue_plate']` |
| YOLO | **1 → 2** | (c-yolo) | `Overriding model.yaml nc=1 with nc=2` → `Detect [2, [128, 256, 512]]` → `Transferred 493/499 items from pretrained weights` | **re-initialised** (the 6 class-score tensors), rest transferred | `base checkpoint classes (1): ['blue_plate']` / `manifest classes (2): ['blue_plate', 'blue_plate_b']` / `class set differs: the detection head will be re-initialised` |
| RF-DETR (rfdetr 1.10.1) | 90 → 1 (published COCO) | (b1) | `Checkpoint has 90 classes but model is configured for 1. The detection head will be re-initialized to 1 classes.` (inside `.train()`, after `_align_num_classes_from_dataset`) | **re-initialised** | `base checkpoint …` line not applicable (published weights; no `BASE_WEIGHTS_S3`) |
| RF-DETR | 1 → 1 (own checkpoint) | (b2) | constructor: `Checkpoint has 1 classes but model is configured for 90. Using checkpoint class count (1). Pass num_classes=1 to suppress this warning.`; `.train()`: **no** `re-initialized` line | kept | `base checkpoint classes match the manifest: ['blue_plate']` |
| RF-DETR | **1 → 2**, `num_classes` **not** pinned (as `build_model` was) | (c-rfdetr) ① | constructor: `Checkpoint has 1 classes but model is configured for 90. Using checkpoint class count (1).`; `.train()`: **`Dataset … has 2 classes but model was initialized with num_classes=1. Using the model's configured value (1).`** | **kept the 1-class head** → `labels [1,300,2]`, mislabelled artifact (bug, fixed) | `base checkpoint classes: ['blue_plate']` / `manifest classes (2): [...]` / `class set differs: rfdetr will re-initialise the detection head` (correct prediction, wrong outcome) |
| RF-DETR | **1 → 2**, `num_classes=2` pinned (fixed `build_model`) | (c-rfdetr) ② | constructor and `.train()` reload: `Checkpoint has 1 classes but model is configured for 2. Using checkpoint class count (1). Pass num_classes=1 …` (misleading wording — see §3.3; the head *is* re-initialised to 3 slots after loading); no `Dataset … has 2 classes` line | **re-initialised** (`class_embed` + 13 `enc_out_class_embed` rows, +14,336 B) → `labels [1,300,3]` | same three lines as ① |

Per arch, what happens and what it means for Req 7.4:

- **ultralytics 8.3.40** — `YOLO(best.pt).train(data=…)` compares the
  checkpoint's `nc` with the `data.yaml`'s; when they differ it logs
  `Overriding model.yaml nc=<old> with nc=<new>`, rebuilds the model from
  the checkpoint's yaml with the new `nc` (so the `Detect` head's class
  branch has fresh out-channels), then `intersect_dicts` transfers every
  tensor whose shape still matches (`Transferred 493/499`), leaving only the
  class-score convs freshly initialised. The `names` for the new head come
  from `data.yaml`, so the exported `best.pt` carries the **new** class list
  and the ONNX output widens to `4 + nc` (`[1,6,33600]` in (c-yolo)).
  Nothing for `train.py` to do beyond the `_log_class_names` old/new line it
  already prints; `IMGSZ` and the rest of the recipe are independent of `nc`.
  The one thing the *portal* should do (Req 7.4 / 6.7) is warn in the UI when
  the chosen base's `names` differ from the manifest's, because the run will
  silently start the class branch from scratch — the numbers in (c-yolo)
  show 3 epochs is nowhere near enough for that branch to converge, so the
  user should expect to train for the full schedule, not a short warm-up.
- **rfdetr 1.10.1** — two distinct places compare the class count, and both
  were exercised:
  1. The **constructor** (`RFDETR*(pretrain_weights=…)`), which defaults to
     `num_classes=90`. If the checkpoint's head width differs and the caller
     did not pin `num_classes`, it adopts the checkpoint's count and logs
     `Using checkpoint class count (N)` — a warning that *looks* alarming but
     is a no-op alignment ((b2), (c-rfdetr)). Passing
     `num_classes=<manifest nc>` here would silence it; `train_rfdetr.py`
     deliberately does not, because `.train()` re-derives it anyway.
  2. **`.train()`** → `_align_num_classes_from_dataset()` sets
     `model_config.num_classes` from `train/_annotations.coco.json`, then the
     Lightning module reloads `pretrain_weights` via `load_pretrain_weights`,
     which compares `class_embed.bias.shape[0]` with `num_classes + 1` and
     calls `reinitialize_detection_head()` when they differ, logging
     `Checkpoint has <old> classes but model is configured for <new>. The
     detection head will be re-initialized to <new> classes.` ((b1) 90 → 1;
     (c-rfdetr) 1 → 2). Because the head is `class_embed` **plus** the 13
     `transformer.enc_out_class_embed.*` group heads, "re-initialised" is a
     larger fraction of the model than YOLO's 6 tensors, but the backbone,
     transformer and box heads are all kept.
  The new class list is stored by our `class_names` kwarg (backed into
  `args.class_names` of the saved `checkpoint_best_total.pth` and into
  `training_metadata.json`), so the exported checkpoint is self-describing
  for the next round. Nothing for `train_rfdetr.py` to do beyond the
  `log_checkpoint_classes` line it already prints.

**Recommendation for Req 7.4 (feeds 1.4 / 7.0):** drop the provisional
"entry points re-initialise the class head" wording — both frameworks own
that, and doing it ourselves would just fight them. Keep 7.4 to (i) the
old-vs-new class log line (done, verified on both arches), (ii) a UI
mismatch warning fed by the base's class names (Req 6.7; names are in the
ultralytics `names` dict / the RF-DETR `args.class_names`, both of which
`classify_checkpoint` recovers — §2), and (iii) writing `class_names` /
`num_classes` into the YOLO `training_metadata.json` too, so the portal
does not have to open `best.pt` to label a prior job's head.

### 3.6 Baseline vs fine-tune, per arch (same manifest, same 22-image test split)

| arch | run | start | epochs run (best) | test mAP@50 | test mAP@50-95 | train/billable s | Δ vs published-start baseline |
|---|---|---|---|---|---|---|---|
| YOLO11s @1280 | baseline `blue-plate-trained-imts-20260913-142306` (portal) | published `yolo11s.pt` | 100 / patience 30 | 0.995 | 0.9192 | 1207 | — |
| YOLO11s @1280 | (a) `tl13-yolo-ft-0453` | R1′ `best.pt` (1-class) | 30 (best 20) | 0.995 | 0.9218 | 592 | mAP@50 =, mAP@50-95 **+0.003**, **−51 %** time |
| RF-DETR small @512 | (b1) `tl13-rfdetr-base-0506` (= baseline) | published `rf-detr-small.pth` (COCO) | 15 (best 5) | 1.000 | 0.9528 | 767 | — |
| RF-DETR small @512 | (b2) `tl13-rfdetr-ft-0533` | (b1) `checkpoint_best_total.pth` (1-class) | ≈13 (best 2) | 1.000 | 0.9610 | 717 | mAP@50 =, mAP@50-95 **+0.008**, −7 % time |
| YOLO11s @1280 | (c-yolo) `tl13-yolo-nc2-0553` — head test only | R1′ (1-class) onto 2-class manifest | 3 | 0.306 | 0.184 | 426 | n/a (synthetic classes) |
| RF-DETR small @512 | (c-rfdetr) ① `tl13-rfdetr-nc2-0603` — head test, **buggy** (1-class head kept) | (b1) (1-class) onto 2-class manifest | 3 | 0.600 | 0.567 | 446 | n/a (synthetic classes; wrong head) |
| RF-DETR small @512 | (c-rfdetr) ② `tl13-rfdetr-nc2b-0625` — head test, fixed | (b1) (1-class) onto 2-class manifest | 3 | 0.568 | 0.539 | 456 | n/a (synthetic classes) |

Reading it: on this dataset a warm start from a same-class checkpoint
matches or nudges the baseline's quality (+0.003 / +0.008 mAP@50-95, well
inside the noise of a 22-image test split) while early stopping fires much
sooner, so the saving is wall-clock, not accuracy — for YOLO roughly half
the job time, for RF-DETR only ~50 s because its fixed overhead (capacity
wait, 10 GB image, 23 s pip, 224 MB base download) dominates a 13- vs
15-epoch difference. RF-DETR small at 512² beats YOLO11s at 1280² on
mAP@50-95 by ~0.03–0.04 in every pairing. Every job stayed far below the
`MaxRuntimeInSeconds=5400` cap.

### 3.5 Done — hand-off to 1.4

Nothing from the 1.3 task text is pending. State a follow-up needs:

- **Jobs** (all `Completed`, all tagged `Spike=tl-1-3`, all far below the
  `MaxRuntimeInSeconds=5400` cap): `tl13-yolo-ft-0453` (a),
  `tl13-rfdetr-base-0506` (b1), `tl13-rfdetr-ft-0533` (b2),
  `tl13-yolo-nc2-0553` (c-yolo), `tl13-rfdetr-nc2-0603` (c-rfdetr, first
  attempt — the mislabelled-head artifact, kept as evidence; **do not use
  it as a base**), `tl13-rfdetr-nc2b-0625` (c-rfdetr ②, the fixed re-run;
  its artifact is a valid 2-class RF-DETR checkpoint). Artifacts under
  `s3://ryvan-cookies/spike/tl-1-3/<job>/output/<job>/output/model.tar.gz`;
  sourcedirs beside them. Nothing is `InProgress`.
- **Code changed in this pass (uncommitted, in the working tree)**:
  `datasets/detection_training/train_rfdetr.py` (`build_model(...,
  num_classes=)` pin on the base-weights path + docstring; `main()` passes
  `len(class_names)`), `datasets/detection_training/train.py` (`export(...,
  class_names=)` so the `channel dim` log line derives its expectation from
  the manifest), `edge-cv-portal/backend/tests/test_train_rfdetr_static.py`
  (two `build_model` tests; suite 44/44 green on the host). Neither entry
  point is preservation-tracked. The RF-DETR fix was validated on SageMaker
  by `tl13-rfdetr-nc2b-0625`; the `train.py` log-line fix was not re-run
  (log text only).
- **Scratch on the build host** (not committed): full CloudWatch logs
  `/tmp/tl-spike/tl13_{a,b1,b2,cyolo,crf,crf2}_cloudwatch.log`; artifacts
  `/tmp/tl13/{b2,cyolo,crf,crf2}_model.tar.gz`; helpers
  `/tmp/tl-spike/tl13_launch.sh`, `tl13_poll.sh`, `tl13_chain.sh`,
  `tl13_make2class.py`; R2′ extracted at
  `/tmp/tl-spike/artifacts/ref_rfdetr_own_checkpoint/`. Host CLI reminder:
  v1 — use `aws logs get-log-events … --start-from-head --limit 10000
  --no-paginate`, not `aws logs tail`.
- **Carry into 1.4 / 7.0** (from §3.3–§3.4): (i) both trainers own the head
  re-init — Req 7.4's entry-point work is the class-name log line (done) plus
  the `num_classes` pin, not a re-init of our own; (ii) tighten
  `verify_two_outputs` to `C+1` for rfdetr 1.10.1; (iii) add
  `num_classes`/`class_names` to the YOLO `training_metadata.json`; (iv)
  the UI mismatch warning (Req 6.7) is the user-facing half of 7.4; (v)
  optionally `settings.update(autoinstall=False)` before the ultralytics
  export to stop the 250 MB `onnxruntime-gpu` AutoUpdate.

## 4. Decisions for Requirement 7 (task 1.4)

Status: **done — 2026-09-15.** Each decision below cites the §1–§3 evidence
it rests on and states the consequence for Requirement 7 / tasks 7.x. The
guiding fact is §1.2: **zero fine-tunable imports exist today** (7/7
`source='imported'` records are ONNX-only; no bare `.pt`/`.pth` anywhere in
the account), and the one real "continue from my detector" case in the
account (I1/I5 ↔ R1) is already served by `resolve_base_model(kind=
'training_job')` (Req 6.3, implemented and tested in 5.1/5.2). Requirement 7
is therefore shaped as the **smallest slice that makes a future checkpoint
import usable as a base**, not as a new import product.

Two code facts established while writing this section (not in §1–§3):

- `model_converter.py`'s convert handler has **no `.pt` → ONNX export**. With
  `export_format='onnx'` the *source* must already be ONNX (it is downloaded
  as `model.onnx` and the torch inspector is skipped); with the default
  `export_format='pytorch'` the source is downloaded as `model.pt`, passed
  through `inspect_pytorch_model` (`torch.load`, `weights_only=False`
  fallback for trusted buckets — §1.4), and `generate_dda_package` copies the
  `.pt` **verbatim** into a legacy Neo/DLR package (`<name>.pt`, manifest
  `framework: PYTORCH`). So an ultralytics `best.pt` or an RF-DETR `.pth`
  put through Smart Import today yields a record whose package the device
  cannot serve, and the checkpoint bytes are buried inside that tarball with
  nothing marking them as fine-tunable. "Keep the checkpoint" is a small
  addition to a path that already holds the bytes, not a new conversion.
- The `imported` branch of `resolve_base_model` (reads
  `metadata.fine_tunable.checkpoint_s3`, arch check, `class_names` fallback
  chain, every 400 message) **already exists and is tested** —
  `tests/test_detection_training_shared.py::test_resolve_base_model_imported_*`
  (8 cases) — and the frontend `FineTunableCheckpoint` type,
  `groupDetectionBaseModels` filter (`metadata.fine_tunable.arch` +
  `checkpoint_s3`) and `baseModelClassNames` fallback landed in 5.3. What
  Requirement 7 still lacks is the **producer** of `metadata.fine_tunable`
  and the Model Detail explanation.

### 4.1 Keep the original checkpoint on Smart Import of `.pt`/`.pth`? — **YES**

- **Decision.** When Smart Import receives a `.pt`/`.pth` (the
  `export_format != 'onnx'` path), run `classify_checkpoint` on the downloaded
  file **before** anything else touches it. If `fine_tunable` is true
  (`ultralytics_checkpoint` → `yolo`, `rfdetr_checkpoint` → `rf_detr`), copy
  the original bytes unchanged to
  `converted-models/<name>-<hex>/checkpoint.<ext>` in the use-case bucket
  (same `<name>-<hex>` as the package, `<ext>` = the source key's extension)
  and persist `metadata.fine_tunable = {arch, kind, checkpoint_s3,
  class_names, num_classes}` on the imported record via the auto-import body.
  Otherwise persist `metadata.fine_tunable = null`. The package the import
  produces is **unchanged** (this decision adds a sidecar object and a
  metadata field; it does not make the `.pt` servable — a fine-tunable import
  is a base-model carrier, and the way to get a device-servable ONNX from it
  is to run a training job from it, which is the whole point of Req 7).
- **Evidence.** §2.3: the probe is 25/25 on every kind including all three
  RF-DETR layouts (published Namespace layout R2, 1.10.1 callback layout R2′,
  PTL-payload layout) and recovers `names`/`nc` for ultralytics (R1, R1′) and
  `args.class_names` + head width for RF-DETR (R2′); ≤ 0.2 s per file, stdlib
  only, never unpickles (§2.1) — so it adds no `torch.load` exposure to a
  Lambda that already has the RCE-sensitive `weights_only=False` fallback.
  §1.2: nothing in the account exercises this path today, so the cost is the
  code + tests + one `model_converter.py` rebaseline, not a migration.
- **Consequence.** Req 7.1 stays, made precise (§5, task 7.2). The S3 write
  is to the same bucket/prefix the Lambda already uploads the package to → no
  IAM change (Req 8.4 holds). `inspect_pytorch_model`'s load policy is **not**
  changed (out of scope; security-reviewed). `model_converter.py` is
  IAM-baseline-tracked (`iam_out_of_scope_baseline.json`) → rebaseline in the
  same commit with a note; `model_import.py` is not tracked.

### 4.2 Accept a paired training-checkpoint upload for ONNX imports? — **NO (deferred)**

- **Decision.** Do not add a second upload field to the Model Import page,
  and do not add an API/S3 path for attaching a checkpoint to an existing
  ONNX record. Req 7.3 is marked out of scope with this reason.
- **Evidence.** §1.2: 7/7 imports are ONNX-only and every one whose ONNX
  came from a portal job (I1, I5 ↔ R1) already has its `best.pt` reachable
  through `kind='training_job'`; the remaining imports (YOLO-World exports,
  `yolov8n.onnx`, an RF-DETR seg ONNX) have **no** checkpoint anywhere in the
  account to pair. There is no demand signal. Cost side: a paired upload
  needs a new upload surface in `ModelImport.tsx`, a new server-side
  validation step (the uploaded bytes are untrusted → must go through
  `classify_checkpoint` + the trusted-source gate), a way to *update* an
  existing record's `metadata`, and a matching-check (does this `.pt` really
  correspond to that ONNX? — unverifiable by inspection), i.e. UI + IAM +
  trust surface for a case nobody has.
- **Consequence.** When a user has both files, the supported path is:
  Smart-Import the checkpoint (§4.1) — that record is the base model; the
  ONNX record stays the deployable. Model Detail's explanation (§4.5) tells
  them exactly that. If demand appears, this reopens as its own small spec.

### 4.3 Reject TorchScript as a base? — **YES, explicitly, with a message**

- **Decision.** `torchscript` is classified (never guessed at), gets
  `fine_tunable = False`, `arch = None`, `metadata.fine_tunable = null`, is
  never offered by the Base model control, and if referenced anyway
  `resolve_base_model` returns the existing 400 `Imported model <name> has no
  fine-tunable checkpoint (ONNX/TorchScript)`. Model Detail explains why.
- **Evidence.** §1.3 R3 / §1.4: a TorchScript zip (`constants.pkl` +
  `code/__torch__/…`) is a frozen graph — `torch.jit.load` is the only
  loader; neither `YOLO(path)` nor `RFDETR*(pretrain_weights=)` can continue
  training it, and it carries no `names`/`nc`. §2.2: the probe identifies it
  from the zip member set alone (1.00/1.00, R3), so the rejection is
  deterministic, not best-effort.
- **Consequence.** Req 7.5's "exclude explicitly rather than best-effort
  load" is satisfied by construction; the UI text is fixed in §4.5.

### 4.4 Which formats are in, which are out, and why

| probe `kind` | arch | fine-tunable | why | evidence |
|---|---|---|---|---|
| `ultralytics_checkpoint` (`best.pt`, full pickled `DetectionModel` or `{'model': …}`) | `yolo` | **in** | `YOLO(path).train()` loads it and re-inits the head itself when `nc` differs (`Transferred 493/499`) | §3.3 (a), (c-yolo); §3.4 |
| `rfdetr_checkpoint` — **all three layouts**: published `{model, optimizer, lr_scheduler, epoch, args(Namespace)}`, 1.10.1 callback `{model, args(dict), epoch, model_name, …}`, and the PTL-payload variant R2′ | `rf_detr` | **in** | `RFDETR*(pretrain_weights=path, trust_checkpoint=True, num_classes=C)` loads all of them; head re-init is the loader's own behaviour once `num_classes` is pinned | §1.4; §2.2 R2, R2′; §3.3 (b2), (c-rfdetr) ② |
| `torchscript` | — | **out** | frozen graph, no trainer can continue it (§4.3) | §1.3 R3 |
| `state_dict` (plain `{state_dict: …}` / raw OrderedDict) | — | **out** | no architecture definition; keys are whatever the author's model had (R4: an LFV ResNet18) — neither trainer can instantiate a model to load it into | §1.3 R4 |
| `legacy_torch` (pre-zip tar) | — | **out** | same as `state_dict`, older envelope; only seen as torchvision's `resnet18-5c106cde.pth` | §1.3 R5 |
| `onnx` | `yolo` when `metadata_props` say so, else `None` | **out** | inference graph; no optimizer state, no trainer path. `num_classes`/`names` are still recovered for the UI where present | §1.2 I1–I7; §2.2 |
| `unknown` (ELF, garbage, truncated, non-existent) | — | **out** | not a checkpoint; probe never raises | §2.1, §2.2 Neo negatives |

Rule of thumb the code enforces: **`fine_tunable` is true iff `kind ∈
{ultralytics_checkpoint, rfdetr_checkpoint}`**, and `arch` is non-null only for
those two plus ultralytics-exported ONNX. The `[1, Q, C]` → `C−1` rule for
RF-DETR ONNX is deliberately **not** added (§2.4 (b)) — ONNX is not a base.

### 4.5 How class names reach the UI

- **Prior portal job (`kind='training_job'`)** — from the record:
  `detection.class_names` is written at job creation from the manifest's
  class-map (R1′'s record: `[blue_plate]`), and `baseModelClassNames` reads it
  (5.3). No file is opened. Gap closed by 7.4: the YOLO
  `training_metadata.json` gains `num_classes` + `class_names` (RF-DETR's
  already has them — §3.2 (b2)) so the artifact is self-describing too and
  the portal never needs `best.pt` to label a prior job's head (§1.5 gap,
  §3.5 (iii)).
- **Import (`kind='imported'`)** — from `metadata.fine_tunable.class_names`,
  filled at import time by `classify_checkpoint`: ultralytics `model.names`
  (always present — R1, R1′), RF-DETR `args.class_names` (present in our own
  checkpoints R2′; **absent** in the published COCO files R2, where only
  `num_classes = 90` is recoverable — §2.5). When names are absent the UI
  shows the count only, and the mismatch warning (Req 6.2) is skipped rather
  than fabricated. `baseModelClassNames` already falls back
  `fine_tunable.class_names → metadata.class_names → detection.class_names`.
- **Not fine-tunable (ONNX / TorchScript / state_dict / legacy)** — Model
  Detail renders, from `metadata.fine_tunable === null` plus the record's
  `metadata.framework` / `pt_file`, one of: *"ONNX graphs cannot be
  fine-tuned. To continue training this model in the portal, Smart-Import
  its training checkpoint (ultralytics `.pt` or RF-DETR `.pth`); that import
  will appear under Base model."* / *"TorchScript models are frozen graphs
  and cannot be fine-tuned."* / *"This file is a bare state_dict without a
  model definition and cannot be fine-tuned."* Fine-tunable imports render a
  "Fine-tunable (YOLO|RF-DETR)" badge with the class list or count.

### 4.6 Other findings that change Req 7 or later work

1. **Head re-init is the trainers' job, not ours** (§3.4). Req 7.4's
   provisional "the entry point SHALL re-initialise the class head" is
   rewritten to: pin the class count and log old vs new. ultralytics does it
   unconditionally (`Overriding model.yaml nc=…`); rfdetr 1.10.1 does it
   **only if `num_classes` is pinned** on the `pretrain_weights` path — the
   silent-keep bug in (c-rfdetr) ① shipped a mislabelled artifact. The pin
   (`build_model(..., num_classes=len(class_names))`) is already in the
   working tree and was validated on SageMaker by `tl13-rfdetr-nc2b-0625`
   (§3.3). Task 7.4 commits it with its static tests.
2. **Tighten `verify_two_outputs` to `C+1`** (§3.3 (c-rfdetr) ①). The "`C`
   or `C+1`" leniency is what let the 1-class head pass for a 2-class
   manifest. rfdetr 1.10.1 always emits `C+1`; the check becomes exact and a
   `C`-slot graph is `FATAL`. Task 7.4.
3. **YOLO `training_metadata.json` gains `num_classes` and `class_names`**
   (§3.3 (c-yolo) note; §3.5 (iii)). Task 7.4; `packaging.py` is unaffected
   (it reads `class_names` from the record).
4. **ultralytics AutoUpdate during export** pulls `onnxruntime-gpu 1.30.0`
   (~250 MB) on every YOLO job (§3.3 (a), (c-yolo)). Harmless today because
   `EnableNetworkIsolation=false`, but it is unpinned network I/O inside a
   training job. Task 7.4 adds `settings.update(autoinstall=False)` before
   `export()` in `train.py`; if the export then fails for lack of a package,
   that package gets pinned in `requirements.txt` instead. Verified by the
   next real YOLO job (task 11 (b)), not by a spike run.
5. **The design.md heuristic for RF-DETR `.pth` was wrong** (§1.4: no
   `rfdetr`/`lwdetr` GLOBALs exist in real files; the signal is
   `class_embed.*` / `enc_out_class_embed.*` / `backbone.0.encoder.*` key
   names plus the `args` field set). design.md's `classify_checkpoint`
   bullet is corrected in 7.0.
6. **Where `classify_checkpoint` lives.** The prototype is ~1,150 lines of
   stdlib; folding it into `detection_training.py` (pure-function contract,
   already 800+ lines) would bury it. It moves to the shared layer as its
   own module `edge-cv-portal/backend/layers/shared/python/checkpoint_probe.py`
   (reachable from the converter Lambda via `/opt/python`, like
   `detection_training`), with `detection_training.classify_checkpoint`
   re-exported so design.md's "lands here" holds. The
   `datasets/detection_training/_checkpoint_probe.py` prototype is deleted
   (the SageMaker entry points do not need it — they already have torch).
7. **`resolve_base_model('imported')` is done** (see the code facts above),
   so provisional 7.4's shared-layer half collapses to "no change"; the
   remaining 7.4 work is all in the entry points.

## 5. Re-planned tasks 7.x (task 1.4)

The text below is what `tasks.md` §7 adopts in task 7.0 (numbering kept so
the dependency graph is unchanged; 7.3 is dropped per §4.2).

- [ ] 7. Imported models as base models (Requirement 7 — re-planned from `docs/transfer-learning-spike.md` §4–§5)
  - [x] 7.0 Re-plan: rewrite 7.1–7.4 below from `docs/transfer-learning-spike.md` (which formats are in, which are excluded, whether Smart Import keeps the checkpoint, whether a paired upload is added). Update requirements.md Req 7 accordingly. Do not implement 7.1–7.4 before this.
    - _Requirements: 5.5_
  - [ ] 7.1 Promote the probe: move `datasets/detection_training/_checkpoint_probe.py` to `edge-cv-portal/backend/layers/shared/python/checkpoint_probe.py` unchanged in behaviour (stdlib only, never raises, `KINDS`/`ARCHES` as in spike §2.1); re-export `classify_checkpoint` from `detection_training.py`; delete the prototype. Fixtures under `edge-cv-portal/backend/tests/fixtures/checkpoints/` built synthetically at test time (small torch-zip envelopes written with `zipfile` + hand-assembled `data.pkl` opcodes: ultralytics `names`/`nc`, RF-DETR published Namespace layout, RF-DETR 1.10.1 dict layout with `args.class_names`, TorchScript `constants.pkl`+`code/`, plain `state_dict`, legacy tar, ONNX header with/without `metadata_props.names`, ELF/garbage/0-byte/truncated). Tests in `tests/test_checkpoint_probe.py`: every kind → expected `{kind, arch, fine_tunable, num_classes, class_names}`; `fine_tunable` true iff kind ∈ {`ultralytics_checkpoint`, `rfdetr_checkpoint`}; RF-DETR `num_classes` from `class_embed.bias` − 1 and never from `args.num_classes`; no exception on any garbage input.
    - _Requirements: 7.1, 7.2, 7.5, 8.2_
  - [ ] 7.2 Smart Import keeps the checkpoint. `edge-cv-portal/backend/functions/model_converter.py` (convert handler, `export_format != 'onnx'` path only): after download and before `inspect_pytorch_model`, `probe = classify_checkpoint(local_model)`; when `probe['fine_tunable']`, `upload_file` the **unmodified** source to `converted-models/<safe_model_name>-<hex>/checkpoint.<source ext>` (same `<hex>` as the package key) in the use-case bucket and set `fine_tunable = {arch, kind, checkpoint_s3, class_names, num_classes}`, else `fine_tunable = None`; include `fine_tunable` in the auto-import body and in the 200 response; leave `inspect_pytorch_model`, `generate_dda_package` and the ONNX path byte-identical. `edge-cv-portal/backend/functions/model_import.py`: accept optional `fine_tunable` in the import body, validate it (`arch ∈ {yolo, rf_detr}`, `kind ∈ {ultralytics_checkpoint, rfdetr_checkpoint}`, `checkpoint_s3` an `s3://<this use case's bucket>/converted-models/…` URI, `class_names` a list of strings or null) → 400 on anything else, and persist `metadata.fine_tunable` (validated dict or explicit `null`; `null` when the field is absent). Tests: `tests/test_model_converter_fine_tunable.py` (moto: ultralytics fixture → sidecar object exists with identical bytes + `fine_tunable` in the import payload; TorchScript/state_dict fixture → no sidecar, `fine_tunable: null`; ONNX path never calls the probe) and `tests/test_model_import_fine_tunable.py` (persisted shape; absent → `null`; wrong bucket/arch/kind → 400). Rebaseline `model_converter.py` sha256 in `test/backend-test/security/baselines/iam_out_of_scope_baseline.json` with a note naming this task.
    - _Requirements: 7.1, 7.2, 7.5, 8.3_
  - [x] 7.3 DROPPED — no paired training-checkpoint upload for ONNX imports: zero demand (7/7 imports ONNX-only, no pairable checkpoint in the account) and it adds UI + trust + record-update surface; the supported path is to Smart-Import the checkpoint itself (spike §4.2). `ModelDetail.tsx` work moved to 7.4.
    - _Requirements: 7.3 (out of scope)_
  - [ ] 7.4 Entry points + Model Detail. (a) `datasets/detection_training/train_rfdetr.py`: keep the `build_model(..., num_classes=len(class_names))` pin on the base-weights path (validated on SageMaker `tl13-rfdetr-nc2b-0625`); tighten `verify_two_outputs` to exactly `[1, Q, C+1]` (a `C`-slot graph is `FATAL`, message quotes both shapes); update `tests/test_train_rfdetr_static.py` (`C` slots → raises, `C+1` → ok, existing `build_model` pin tests stay). (b) `datasets/detection_training/train.py`: write `num_classes` and `class_names` into `training_metadata.json` (from the converter's `data.yaml` names); `from ultralytics import settings; settings.update(autoinstall=False)` before `model.export(...)`; new static suite `tests/test_train_yolo_static.py` (pattern of `test_train_rfdetr_static.py`: import the module with `ultralytics` stubbed) asserting the metadata dict carries `num_classes`/`class_names` and that `settings.update(autoinstall=False)` runs before `model.export`. Both entry points keep their old-vs-new class log lines; neither re-initialises a head itself. (c) `edge-cv-portal/frontend/src/pages/ModelDetail.tsx`: for `source === 'imported'`, render a "Fine-tunable (YOLO|RF-DETR)" badge with class names (or count when names are absent) when `metadata.fine_tunable` is set, otherwise the per-kind explanation from spike §4.5 (ONNX → "Smart-Import its training checkpoint (.pt/.pth)…", TorchScript → frozen graph, state_dict → no model definition); test `ModelDetail.fineTunable.test.tsx` covering the three texts and the badge. No `resolve_base_model` change (the `imported` branch and its 8 tests already exist).
    - _Requirements: 7.2, 7.4, 7.5, 6.3, 8.2_
