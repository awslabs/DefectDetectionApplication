# RF-DETR-Seg support: exploration spike (task 0.2)

Spec: `.kiro/specs/rfdetr-seg-support` (design "Spike (task 0)", S1–S7; tasks.md 0.2). Run on
2026-11-22 in the lab Portal account (us-east-1) on branch `feature/rfdetr-seg-support`, before any
code task. Throwaway scripts against the rfdetr 1.10.1 API only; the only repository code they ran
is the unchanged `checkpoint_probe.classify_checkpoint` and `_common.cap_onnx_ir_version`. Nothing
from the spike is kept under `datasets/`, `edge-cv-portal/` or `src/`; the scripts and raw outputs
live with the run records (§7).

Two runs, as the plan asked:

- **Run (i)** — inside the **deployed** Export_Image (`dda-detector-export@sha256:2b6dba2a…`, the
  digest the lab ModelConverter Lambda and the CDK SSM parameter point at), pulled by digest onto an
  idle x86_64 Build Fleet server (16 vCPU, no other build running) and started with `docker run --rm
  --entrypoint bash <image> -c "python /scratch/spike_export.py; /opt/ort-floor/bin/python
  /scratch/spike_floor.py"` over SSM, a bind-mounted scratch directory, network on (the weights
  download). Image python 3.11.16, torch 2.5.1+cpu, rfdetr 1.10.1, onnx 1.23.0, onnxruntime
  **1.30.0**; `/opt/ort-floor`: onnxruntime **1.16.3**, numpy 1.26.4. Wall time 63 s.
- **Run (ii)** — one SageMaker training job on the trainer DLC (`pytorch-training:2.5.1-gpu-py311-
  cu124-ubuntu22.04-sagemaker`, `ml.g4dn.xlarge`, Tesla T4, the Portal's `DDASageMakerExecutionRole`,
  60 GB volume, `EnableNetworkIsolation=False`), sourcedir = `spike_train.py` + the unchanged
  `_common.py` + `requirements.txt` copied from `datasets/detection_training/requirements-rfdetr.txt`
  (so the job installs the trainer's own pins: onnx 1.22.0, onnxruntime **1.19.2**, numpy 1.26.4),
  dataset = a hand-built two-class COCO set with uncompressed RLE `segmentation` dicts (40 synthetic
  320×320 images: 28 train / 6 valid / 6 test, 74 instances, `{"counts": [...], "size": [h, w]}`
  column-major, background run first — WQ-req-author-1's format; task 3.2's converter was not yet
  written). Three attempts: attempt 1 failed at `import rfdetr` (S5 finding below), attempt 2 ran
  every step with one added pin, attempt 3 re-ran it with two extra read-only checks (S1b, S2b).
  Billable time 300 s per attempt; wall time ≈ 7.5 min from `CreateTrainingJob` to `Completed`.

## 1. Outcome

| # | Question | Design default | Observed | Verdict |
|---|---|---|---|---|
| S1 | The published Seg-Nano `.pth`: probe label and evidence; `segmentation_head.*` keys; `args.segmentation_head`; `num_classes`; `model_name` | `rfdetr_checkpoint`, two task signals, `args_picks` | `kind = rfdetr_checkpoint`, `arch = rf_detr`, `fine_tunable = True`, `num_classes = 90`; **35** `segmentation_head.*` keys, 0 `mask_` keys; the file is a **stripped `{"model"}`-only** checkpoint — no `args`, no `model_name`, no `rfdetr_version` (`zip_root = checkpoint_best_regular_stripped`), so `args.segmentation_head` and every `args_pick` are **absent** for published weights; a 1.10.1 **training** checkpoint carries `args`, `model_name` and `rfdetr_version` (S1b) | Confirmed with one amendment for 0.3: the probe's task signal for published weights is the state dict alone; size evidence is absent (unknown-size path of 7.2). Fixture builder gains the stripped shape. |
| S2 | Output names and static dims as loaded by three runtimes; stamped `ir_version` before / after the cap; the reference returns a 3-tuple | names present, dims static; `out[:3]` | Names `dets`, `labels`, `masks` in every runtime and in the protobuf. `dets [1, Q, 4]` and `labels [1, Q, C+1]` static everywhere (`[1, 100, 91]` published, `[1, 100, 3]` after the two-class fine-tune — the trailing background slot holds for a fine-tuned Seg model). **`masks` is declared with four symbolic dims (`Addmasks_dim_0..3`) in the graph output**; onnxruntime 1.30.0 reports it static `[1, 100, 78, 78]`, onnxruntime **1.19.2 and 1.16.3 report the symbolic names**; the run output is `[1, 100, 78, 78]` float32 in all three. `onnx.shape_inference.infer_shapes` resolves only `Q`. Stamping the run-time shape into `graph.output` passes `onnx.checker`, and all three runtimes then report static dims with byte-identical outputs. IR stamped **8**, opset 17 (producer `pytorch 2.5.1`); `cap_onnx_ir_version` → `(8, 8)`; 1.16.3 loads the raw graph. The reference (`forward_export`) returns a **tuple of 3**. | Names: confirmed. Dims: **not static in the protobuf** — 0.3 re-plan (M-1): one post-export static-dim stamp before every protobuf consumer. 1.16.3 **loads and runs** the graph: the JP5 / x86_64 / arm64_cpu coverage claim stands; the stop rule is not triggered. |
| S3 | `.evaluate()` carries the two mask keys after a real 2-epoch run | `_SEG_EVAL_KEY_MAP` of §4 | `rfdetr evaluate(test)` keys: `test/loss`, `test/mAP_50_95`, `test/mAP_50`, `test/mAP_75`, `test/mAR`, `test/precision`, `test/recall`, `test/F1`, **`test/segm_mAP_50`**, **`test/segm_mAP_50_95`** (values 0.698 / 0.629 on the synthetic test split) | Confirmed. |
| S4 | Mask IoU, torch reference vs ONNX, both image runtimes | 0.99 | bus.jpg (the image bundled with ultralytics): 7 matched queries (reference max score > 0.3), **min IoU 1.0, mean 1.0** on 1.30.0 and on 1.16.3; max abs diff vs reference — boxes 1.6e-4 / 1.9e-4, logits 4.7e-4 / 4.4e-4, mask logits 6.7e-3 / 6.6e-3; 1.30.0 vs 1.16.3: mask logits ≤ 1.3e-2. The seeded synthetic tensor matched no query (top score 0.21). | `PARITY_TOLERANCE['rf_detr_seg']['mask_iou'] = 0.99` holds (7.4). |
| S5 | Does `rfdetr[train,onnx]==1.10.1` cover the Seg trainer / exporter; pin changes? | pins unchanged | Export_Image: `import rfdetr; from rfdetr import RFDETRSegNano; import rfdetr.export` OK, `pip check` clean (its lock pins `transformers==5.17.0`). **Trainer DLC: `requirements-rfdetr.txt` as committed does not install today** — pip resolves rfdetr's unpinned `transformers<6.0.0,>=5.1.0` to **5.24.0**, and `import rfdetr` dies at `models/backbone/dinov2_with_windowed_attn.py:43`: `ImportError: cannot import name 'BackboneConfigMixin' from 'transformers'`. With one added line, `transformers==5.17.0`, the install, import, training, evaluation and export all work. The extras themselves cover the Seg trainer and exporter (`pycocotools`, `faster-coco-eval`, `pytorch_lightning`, `torchmetrics`, `onnxsim` all came from `[train,onnx]`). | **One pin change** for `requirements-rfdetr.txt` (M-3); the Export_Image lock needs none. The breakage is pre-existing and also stops the RF-DETR **detection** trainer, which installs the same file. |
| S6 | Decode cost at 1920×1080, 10 and 100 instances (x86, labelled) | §24 numbers | x86_64 Build Fleet server, in-container CPU: **10 instances 49 ms** (bilinear resize + threshold + `packbits` 20 ms, `unpackbits` + paint 6 ms, RLE encode 22 ms); **100 instances 447 ms** (177 / 58 / 212 ms); packed mask 259,200 B per instance; RLE text ≈ 4.6 KB per synthetic blob instance. | Recorded for §24 (task 15.1) as the x86 figure; the JP6 figure comes from 22.x. |
| S7 | Do the six published Seg weights resolve? | all six available | All six resolve in both environments through rfdetr's resolver into `~/.roboflow/models/`: `rf-detr-seg-nano.pt` 134.5 MB (312 px, Q 100, 1 window, 4 decoder layers), `-small.pt` 135.0 MB (384, 100, 2, 4), `-medium.pt` 143.0 MB (432, 200, 2, 5), `-large.pt` 145.1 MB (504, 200, 2, 5), `-xlarge.pt` 152.7 MB (624, 300, 2, 6), `-xxlarge.pt` 154.9 MB (768, 300, 2, 6); every one `patch_size 12`, `encoder dinov2_windowed_small`. | Confirmed; no README caveat needed (15.2). |

Design defaults hold except where the table says "0.3": the mask-dim stamp (M-1), the stripped
published checkpoint (M-2) and the `transformers` pin (M-3). None touches an owner decision.

## 2. Run (i), item by item

### S1 — probe of the published Seg-Nano `.pth`

`RFDETRSegNano()` downloaded `rf-detr-seg-nano.pt` (134,545,398 bytes) into the container's
`~/.roboflow/models/`. `checkpoint_probe.classify_checkpoint(path)` (unchanged, copied beside the
script):

```json
{"kind": "rfdetr_checkpoint", "arch": "rf_detr", "fine_tunable": true, "num_classes": 90,
 "evidence": {"container": "zip", "zip_root": ["checkpoint_best_regular_stripped"],
              "top_level_keys": ["model"], "pickle_ops": 20954,
              "rfdetr_signals": {"class_embed": true, "enc_out_class_embed_groups": 13,
                                 "backbone.0.encoder.*": 223, "args_fields": [], "args_type": "NoneType",
                                 "state_dict_prefix": "", "n_tensors": 544},
              "num_classes_source": "class_embed.bias.shape[0] - 1"}}
```

`torch.load(path, map_location='cpu')`: top-level keys `["model"]` only; state-dict prefixes
`backbone, bbox_embed, class_embed, query_feat, refpoint_embed, segmentation_head, transformer`;
`segmentation_head.*` = 35 tensors (`bias`; `blocks.{0..3}.{dwconv,norm,pwconv1}.{weight,bias}`;
`query_features_block.{layers.0,layers.2,norm_in}.{weight,bias}`; `query_features_proj.*`;
`spatial_features_proj.*`); `class_embed.bias` shape `[91]`. No `args`, so `args.segmentation_head`,
`patch_size`, `num_windows`, `resolution`, `num_queries`, `encoder` are not recoverable from this
file — the state dict is the only task signal and there is no size signal besides tensor shapes.
What 7.1's fixture builder must reproduce: the detection builder plus the 35 `segmentation_head.*`
tensors, **with and without** `args` / `model_name` (S1b below shows the training-checkpoint shape).

### S2 — the export, in the image's own onnxruntime and in `/opt/ort-floor`

`model.export(output_dir=..., opset_version=17, batch_size=1, dynamic_batch=False, format="onnx",
verbose=False, output_name="model")` — the kwargs `train_rfdetr.export_onnx` passes — took 2.1 s on
CPU and wrote one file, `model.onnx`, 122,824,238 bytes.

`onnx.load`: input `input [1, 3, 312, 312]`; outputs `dets [1, 100, 4]` FLOAT, `labels [1, 100, 91]`
FLOAT, `masks ['Addmasks_dim_0', 'Addmasks_dim_1', 'Addmasks_dim_2', 'Addmasks_dim_3']` FLOAT;
`ir_version 8`; `opset_import [('', 17)]`; producer `pytorch 2.5.1`.

| Runtime | `onnxruntime.__version__` | `get_outputs()` shapes | run output shapes |
|---|---|---|---|
| Export_Image (own) | 1.30.0 | `dets [1,100,4]`, `labels [1,100,91]`, `masks [1,100,78,78]` | `[1,100,4]`, `[1,100,91]`, `[1,100,78,78]` |
| `/opt/ort-floor` (Fleet_Floor_Runtime) | 1.16.3 | `dets [1,100,4]`, `labels [1,100,91]`, `masks ['Addmasks_dim_0', …, 'Addmasks_dim_3']` | same |
| Trainer DLC, run (ii) | 1.19.2 | `dets [1,100,4]`, `labels [1,100,3]`, `masks ['Addmasks_dim_0', …, 'Addmasks_dim_3']` | `[1,100,4]`, `[1,100,3]`, `[1,100,78,78]` |

IR: `_common.cap_onnx_ir_version(copy)` returned `(8, 8)` — a no-op, the export is already below the
≤ 10 ceiling; copies with `ir_version` forced to 9 and 8 (what the conversion job's
`normalize_ir_version` would write above `FLEET_MAX_IR = 9`) load identically on 1.16.3. The
`_common.py` note about onnx 1.22 / 1.23 re-stamping IR 13 / 14 does not apply to rfdetr's
onnxsim pass: onnx 1.23.0 in the image left IR 8.

Follow-up on the symbolic mask dims (`spike_dims.py`): `onnx.shape_inference.infer_shapes` turns
the declaration into `['Addmasks_dim_0', 100, 'Addmasks_dim_2', 'Addmasks_dim_3']` (only `Q`
resolved, 0.4 s). Writing the run-time shape into the graph output —

```python
sess = ort.InferenceSession(path, providers=["CPUExecutionProvider"])
run_shapes = {o.name: list(a.shape) for o, a in zip(sess.get_outputs(), sess.run(None, {"input": zeros}))}
for o in model.graph.output:
    for d, v in zip(o.type.tensor_type.shape.dim, run_shapes[o.name]):
        d.ClearField("dim_param"); d.dim_value = int(v)
onnx.checker.check_model(model); onnx.save(model, path)
```

— keeps IR 8, shrinks the file by 56 bytes, and afterwards 1.30.0, 1.16.3 and 1.19.2 all report
`masks [1, 100, 78, 78]` from `get_outputs()` with outputs byte-identical to the unstamped graph's
(`np.array_equal` on every output, bus.jpg input). This is the remedy proposed under M-1 for 0.3:
every protobuf reader in the plan (`verify_seg_outputs` through `read_onnx_io`, `onnx_summary` →
`verify_rfdetr_seg_contract` / `check_onnx_against_record`, `inspect_onnx_model`) would otherwise see
the symbolic dims and reject a real export's mask output.

The reference call: `deepcopy(model.model.model).eval()`, `.export()`, then one forward on the
preprocessed tensor returns a **tuple** of three tensors `[1,100,4]`, `[1,100,91]`, `[1,100,78,78]`
(DD17 / WQ-design-author-3 confirmed live).

### S4 — mask IoU, reference vs export

Inputs as the conversion job builds them: the seeded synthetic tensor (`PARITY_SEED`, ImageNet
normalised) and the bundled `ultralytics/assets/bus.jpg` (square resize to 312, /255, ImageNet
mean/std). The ~10-line numpy term 7.4 adopts verbatim:

```python
def mask_iou_stats(ref, got, score_floor=0.3, mask_threshold=0.5):
    ref_boxes, ref_logits, ref_masks = ref; got_boxes, got_logits, got_masks = got
    scores = sigmoid(ref_logits[0]).max(axis=1)              # [Q]
    ious = []
    for q in np.flatnonzero(scores > score_floor):
        a = sigmoid(ref_masks[0, q]) > mask_threshold
        b = sigmoid(got_masks[0, q]) > mask_threshold
        union = (a | b).sum()
        ious.append(float((a & b).sum() / union) if union else 1.0)
    return min(ious) if ious else None
```

| Input | Runtime | matched queries | mask IoU min / mean | max abs diff boxes / logits / mask logits |
|---|---|---|---|---|
| bus.jpg | 1.30.0 | 7 | 1.0 / 1.0 | 1.6e-4 / 4.7e-4 / 6.7e-3 |
| bus.jpg | 1.16.3 | 7 | 1.0 / 1.0 | 1.9e-4 / 4.4e-4 / 6.6e-3 |
| synthetic | 1.30.0 | 0 (top score 0.21) | — | 2.9e-5 / 8.0e-5 / 3.1e-3 |
| synthetic | 1.16.3 | 0 | — | 3.0e-5 / 4.6e-4 / 9.8e-3 |

No NaN / Inf on either runtime. The pairing is by query index (same graph, same input); the
conversion job's `compare_rfdetr_parity` pairs by shape and position, which coincides here.

### S5 — import smoke and pins (image half)

`import rfdetr; from rfdetr import RFDETRSegNano; import rfdetr.export` OK; `pip check`: "No broken
requirements found". Pins observed: rfdetr 1.10.1, torch 2.5.1+cpu, torchvision 0.20.1+cpu, onnx
1.23.0, onnxruntime 1.30.0, onnxsim 0.7.3, onnxslim 0.1.96, numpy 2.4.6, opencv-python 5.0.0.93,
pillow 12.3.0, supervision 0.30.5, transformers 5.17.0, ultralytics 8.4.162, scipy 1.17.1;
`pycocotools`, `lightning`, `torchmetrics` absent (train extra only — the exporter does not need
them). The Export_Image lock stays as it is.

### S6 — decode cost at 1920×1080 (x86_64, labelled)

Synthetic per-query logit maps on the native 78×78 grid, each with one blob; per instance
`cv2.resize(..., (1920, 1080), INTER_LINEAR)` → `> 0.0` (≡ sigmoid > 0.5) → `np.packbits`; then
`np.unpackbits(..., count=h*w)` + paint into one uint8 index mask; then the vectorised column-major
RLE of DD5 (`flat = mask.T.reshape(-1)`; `np.diff` over the change points; leading `0` when the first
pixel is foreground).

| instances | resize + threshold + packbits | unpackbits + paint | RLE encode | total | RLE text |
|---|---|---|---|---|---|
| 10 | 20 ms | 6 ms | 22 ms | **49 ms** | 46 KB |
| 100 | 177 ms | 58 ms | 212 ms | **447 ms** | 458 KB |

Packed mask: 259,200 B per instance (1 bit/pixel, DD3). Environment: the x86_64 Build Fleet server's
CPU inside the Export_Image container, 16 vCPU — not a Jetson; task 22.x records the JP6 figure.

### S7 — the six sizes (image half)

See the Outcome table: all six constructors downloaded and loaded their weights (1.1–10.6 s each).

## 3. Run (ii), item by item

### S5 — the pip log (job half) and the `transformers` finding

Attempt 1 (`rfdetr-seg-spike-20261122-180337`, sourcedir `requirements.txt` = the committed
`requirements-rfdetr.txt`): pip collected `transformers<6.0.0,>=5.1.0` for rfdetr 1.10.1 and
installed **transformers-5.24.0** (with huggingface-hub-2.4.0, tokenizers-0.23.3, pytorch_lightning-
2.6.6, torchmetrics-1.8.2, faster-coco-eval-1.8.1, pycocotools-2.0.11, supervision-0.32.0,
onnxsim-0.8.1, peft-0.22.0, onnx-1.22.0, onnxruntime-1.19.2). Every step then failed in the first
4 s:

```
File ".../rfdetr/models/backbone/dinov2_with_windowed_attn.py", line 43, in <module>
    from transformers import (  # type: ignore[attr-defined] # public API; stable across all transformers v5.x
ImportError: cannot import name 'BackboneConfigMixin' from 'transformers'
```

Attempt 2 (`rfdetr-seg-spike-20261122-181533`): the same file plus one line, `transformers==5.17.0`
(the Export_Image lock's version). pip installed transformers-5.17.0, huggingface-hub-1.33.0,
tokenizers-0.23.3 and the same train / onnx extras; `import rfdetr; from rfdetr import RFDETRSegNano;
import rfdetr.export` OK on the DLC (torch 2.5.1+cu124, CUDA, Tesla T4). Because the committed file
is also what every Portal RF-DETR **detection** training job installs, the detection trainer fails
the same way today; the pin is recorded as M-3 for 0.3 / 4.1.

### S7 — the six sizes (DLC)

All six constructors resolve on the DLC (2.8–4.5 s each, same files and sizes as in run (i));
`dec_layers` 4 / 4 / 5 / 5 / 6 / 6. rfdetr logs two warnings per construction — "Using a different
number of positional encodings than DINOv2" and "Using patch size 12 instead of 14 … not loading
DINOv2 backbone weights. This is not a problem if finetuning a pretrained RF-DETR model" — and, for
the stripped published weights, "load_pretrain_weights: checkpoint lacks args.num_queries /
args.group_detr; falling back to flat slice". Expected noise, not failures.

### S3 — the 2-epoch run and `evaluate()`

`RFDETRSegNano().train(dataset_dir=…, epochs=2, batch_size=4, grad_accum_steps=1, lr=1e-4,
output_dir=…, resolution=312, early_stopping=False, run_test=True, tensorboard=False, wandb=False,
class_names=['disc', 'block'])` — 25.7 s on the T4 (rfdetr logged "Checkpoint has 90 classes but
model is configured for 2. The detection head will be re-initialized to 2 classes."; the dataset
has `category_id` 1 and 2, and `model_config.num_classes` was 2 after training). Output directory:
`checkpoint_best_ema.pth`, `checkpoint_best_total.pth`, `last.ckpt`, `last_ema.pth`, `metrics.csv`,
`training_config.json`. Then `model.evaluate(split="test", dataset_dir=…, output_dir=…)` (1.9 s)
printed:

```
rfdetr evaluate(test): {"test/loss": 9.3604, "test/mAP_50_95": 0.6818, "test/mAP_50": 0.6985,
 "test/mAP_75": 0.6985, "test/mAR": 0.6950, "test/segm_mAP_50_95": 0.6288, "test/segm_mAP_50": 0.6985,
 "test/F1": 0.4762, "test/precision": 0.4545, "test/recall": 0.5}
```

Both mask keys are present as WQ-design-author-2 read them from the source (attempt 3, a second
training run on the same data: `test/segm_mAP_50` 0.797, `test/segm_mAP_50_95` 0.736 — the values
vary run to run, the key set does not). The RLE dataset trained without a decode error, so the
`{"counts": [...], "size": [h, w]}` form is accepted end to end.

### S1b — what a 1.10.1 training checkpoint carries (added in attempt 3)

Recorded from the job's own output directory (`torch.load`, attempt 3):

| File | bytes | top-level keys | `model_name` | `rfdetr_version` | `args` | `segmentation_head.*` | `class_embed.bias` |
|---|---|---|---|---|---|---|---|
| `checkpoint_best_total.pth` (the sidecar `CHECKPOINT_MEMBER_FOR_ARCH` ships) | 133,355,788 | `args, best_total_source, callbacks, global_step, loops, lr_schedulers, model, model_name, optimizer_states, pytorch-lightning_version, rfdetr_version, state_dict` | `RFDETRSegNano` | `1.10.1` | dict, 94 fields | 35 | `[3]` |
| `checkpoint_best_ema.pth` | 133,369,108 | the same plus `epoch` and **`model_config`** | `RFDETRSegNano` | `1.10.1` | dict, 94 fields | 35 | `[3]` |

The `args` dict is the serialised **TrainConfig** (`lr`, `epochs`, `dataset_dir`, …, `class_names =
['disc', 'block']`), not the model namespace: `segmentation_head`, `patch_size`, `num_windows`,
`resolution`, `num_queries`, `num_select`, `encoder`, `num_classes`, `group_detr`, `dec_layers` and
`pretrain_weights` are **not keys of `args`** in a 1.10.1 PTL checkpoint. Those model fields live in
`model_config` (`dec_layers, encoder, hidden_dim, mask_downsample_ratio, model_name, num_classes,
num_queries, num_select, num_windows, out_feature_indexes, patch_size, positional_encoding_size,
pretrain_weights, projector_scale, resolution, …`), which the EMA file carries and the stripped
`checkpoint_best_total.pth` does not. rfdetr's own loader copes the same way: `validate_checkpoint_
compatibility` skips the `segmentation_head` check when `args` lacks it and infers `patch_size` from
the state dict (`backbone.0.encoder.encoder.embeddings.patch_embeddings.projection.weight`, a
`[hidden, 3, 12, 12]` tensor for every Seg size). Consequences for §7 / 7.1, folded into M-2: the
probe's authoritative task signal is the state dict (35 `segmentation_head.*` keys — and the
projection weight's 12-pixel patch as the cheap corroboration the design already names), `model_name`
is the size signal for DDA-trained checkpoints, `model_config` is a third optional source of the
"args picks" when present, and `args.segmentation_head` is `None` for both published and 1.10.1
training checkpoints (recorded, never a "disagreement"). The fixture builder therefore reproduces
three envelopes: stripped `{"model"}` (published), `{"model", "args" = TrainConfig, "model_name",
"rfdetr_version", "state_dict", …}` (DDA-trained `checkpoint_best_total.pth`) and the same plus
`model_config` (EMA); the `export_for_roboflow` shape `{"model", "args"}` of WQ-design-author-1 is a
fourth, legacy-`args` case.

### S2 — the trainer-side half: export, `ir_version`, the cap, onnxruntime 1.19.2

`model.export(...)` with the same kwargs, on the trained two-class Seg-Nano: 4.3 s, `model.onnx`
122,643,302 bytes, input `[1, 3, 312, 312]`, outputs `dets [1, 100, 4]`, `labels [1, 100, 3]`,
`masks ['Addmasks_dim_0', …, 'Addmasks_dim_3']`, **`ir_version` stamped 8**, opset 17. onnxruntime
1.19.2 loads the uncapped graph (`get_outputs()`: `dets [1,100,4]`, `labels [1,100,3]`, `masks`
symbolic) and runs it to `[1,100,4]`, `[1,100,3]`, `[1,100,78,78]`; `_common.cap_onnx_ir_version`
returned `(8, 8)` and the capped copy loads and runs identically with finite outputs. So the cap is
not load-bearing for a 1.10.1 Seg export on this DLC (both files load), the trailing background slot
is confirmed for a fine-tuned Seg model (`C + 1 = 3`), and `read_onnx_io` on 1.19.2 would return the
symbolic mask dims — M-1 applies to 4.2 as much as to the Portal readers. Attempt 3 applied the
static-dim stamp of §2 on the DLC (`S2b_stamp_static_dims_dlc`): after it, onnxruntime 1.19.2's
`get_outputs()` reports `masks [1, 100, 78, 78]`, the graph output is declared static, IR stays 8,
and the outputs on a zero tensor are identical to the unstamped graph's — the same evidence as for
the two image runtimes.

## 4. What 0.3 re-plans from this (recorded in the run's `mismatches.md` as M-1 … M-3)

1. **M-1, mask dims.** Add a post-export static-dim stamp (the snippet in §2 S2) to the Seg entry
   point (4.1, before `cap_onnx_ir_version` and `verify_seg_outputs`) and to the conversion job
   (7.3, before the two runtimes); amend Requirement 4.5 / the Seg_Output_Contract glossary entry
   ("static after the exporter's shape stamp; the raw 1.10.1 export declares the mask dims
   symbolically"); give 8.1 a tolerant branch for a foreign unstamped export (rank-4 output with
   symbolic dims → `mask_grid` inferred as `network_input // 4`, hint says so).
2. **M-2, stripped published weights.** 7.1's fixture builder gains the `{"model"}`-only shape; the
   probe records `args_segmentation_head = None` (no disagreement) and `rfdetr_seg_size = None`;
   7.2's unknown-size path (the user's native input, `RFDETR_SEG_SIZE_FOR_NATIVE`) covers it.
3. **M-3, `transformers` pin.** `requirements-rfdetr.txt` gains `transformers==5.17.0` (4.1); the
   Export_Image lock already has it (no 17.1 change); the detection trainer is repaired by the same
   line; `test_requirements_rfdetr_pins` passes unmodified.
4. No change: S3's key map, S4's 0.99 tolerance, S7's six sizes, the 1.16.3 coverage claim for
   JP5 / x86_64 / arm64_cpu, the IR cap (a no-op here, kept as the ceiling the requirement names).

## 5. Facts for later tasks

- Exported Seg-Nano ONNX ≈ 123 MB (both the published 91-slot and the fine-tuned 3-slot heads);
  `onnx.load` 0.4 s; export 2–4 s on CPU / T4.
- A 2-epoch Seg-Nano run on 28 images: 26 s training + 2 s evaluation on a T4; the job's wall time
  is dominated by the DLC pull and the pip install (≈ 5 min), 300 s billable.
- The published weights land in `~/.roboflow/models/<name>.pt` (`RFDETR.maybe_download_pretrain_weights`).
- onnxruntime session metadata resolves symbolic output dims only in newer releases (1.30.0 did,
  1.19.2 and 1.16.3 did not); never read output shapes from `get_outputs()` or the protobuf for a
  Seg graph without the stamp.

## 6. Cleanup

The scratch directory and the three IR-clamped / stamped ONNX copies on the Build Fleet server, the
pulled image, the SageMaker sourcedirs, datasets and `model.tar.gz` outputs under the lab use case
bucket's spike prefix, and the three completed training jobs' records are left for task 0.3, which
deletes the scratch prefix and the sourcedir once the re-plan is committed (tasks.md 0.3). No
Portal record, component or device was touched.

## 7. Scripts and raw outputs

Kept with the implementation run's records (not in the repository): `spike/run1/spike_export.py`
(S1, S2 image half, S4, S5, S6, S7), `spike/run1/spike_floor.py` (S2 / S4 on `/opt/ort-floor`),
`spike/run1/spike_dims.py` + `spike_dims_floor.py` (the dim follow-up), `spike/run2/build_dataset.py`
(the RLE dataset), `spike/run2/spike_train.py` (S3, S5, S7, S1b, S2, S2b on the DLC), the SSM /
SageMaker launch and poll helpers, and the raw outputs `spike_export.json`, `spike_floor.json`,
`spike_dims.json`, `spike_dims_floor.json`, `spike_train.json` (attempts 1–3) and the jobs'
CloudWatch logs. The values quoted above are copied from those files.
