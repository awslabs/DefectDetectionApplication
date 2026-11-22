#!/usr/bin/env python3
"""SageMaker script-mode entry point: fine-tune an RF-DETR-Seg instance
segmentation model on a DDA Instance_Manifest and export ONNX for the DDA
edge runtime (spec rfdetr-seg-support, Requirement 4; design section 4).

Sibling of train_rfdetr.py (RF-DETR detection), which stays untouched: this
module imports it as `det` and reuses its pure helpers (base weights,
`.train()` kwargs, export, ONNX introspection, input check); only what
differs for the segmentation task lives here:

  1. download the labeling job's output manifest (+ images) from S3 and
     refuse anything but an instance manifest (first line carries the
     `instance-segmentation` attribute, or a Ground Truth companion of type
     groundtruth/instance-segmentation) -- a bbox-only or anomaly-mask-ref
     manifest cannot train RF-DETR-Seg
  2. build an RF-DETR ("roboflow") COCO dataset with RLE segmentations via
     the bundled manifest_to_detector_dataset.py --format coco --coco-layout
     rfdetr (train/valid/test/_annotations.coco.json)
  3. fine-tune RFDETRSeg{Nano,Small,Medium,Large,XLarge,2XLarge} from the
     published COCO weights or from BASE_WEIGHTS_S3 (a prior job's
     checkpoint_best_total.pth), the manifest's class count pinned
  4. evaluate the best checkpoint on the leakage-safe `test` split and print
     one `TEST METRICS: {json}` line: the four box keys of train_rfdetr.py
     plus test_mask_map50 / test_mask_map50_95
  5. export ONNX from checkpoint_best_total.pth at RESOLUTION, static batch 1;
     stamp the run-time output shapes into the graph (the raw rfdetr 1.10.1
     export declares the four `masks` dims symbolically -- spike S2); cap
     the IR version; verify exactly three outputs [1, Q, 4] boxes +
     [1, Q, C + 1] logits + [1, Q, R/4, R/4] masks
  6. write model.onnx + checkpoint_best_total.pth + training_metadata.json
     to /opt/ml/model

Geometry contract is RF-DETR's: SQUARE RESIZE (no letterbox) with ImageNet
mean/std normalisation -> the device manifest says preserve_aspect=false,
normalize=true. Class layout is RF-DETR's too (label i == class_names[i];
the LAST logit slot is the never-positive background slot, so `labels` is
[1, Q, C + 1] and a C-slot graph is FATAL). The mask head predicts on a
RESOLUTION // 4 grid (rfdetr `mask_downsample_ratio`); the device
thresholds the sigmoid at mask_threshold (0.5, written to the hints only --
no threshold is passed to the job).

Seg sizes are a SEPARATE table from det.SIZE_CLASSES: six sizes, DINOv2
patch 12 with 1 window (nano) or 2 windows (every other size), so RESOLUTION
must be a multiple of 12 (nano) / 24 (others) in [240, 1152] -- never the
detection rule of 32 (312, 432, 504 and 624 are not multiples of 32).

rfdetr API pins (1.10.1, confirmed live by the spike recorded in
docs/rfdetr-seg-spike.md): RFDETRSeg*() constructors with the detection
kwargs; .train() with det.train_kwargs (TrainConfig is extra="forbid"; the
seg loss knobs stay at SegmentationTrainConfig defaults); .evaluate() ->
flat {"test/mAP_50", ..., "test/segm_mAP_50", "test/segm_mAP_50_95"};
.export() -> model.onnx with outputs dets, labels, masks (in that order),
`masks` declared with symbolic dims in the protobuf although every runtime
produces [1, Q, R/4, R/4] -- hence stamp_static_output_dims below.

rfdetr / torch / onnx / onnxruntime / numpy are imported lazily inside
functions so this module imports on a bare host (unit tests).
"""
import json
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Optional, Sequence, Tuple

import train_rfdetr as det
from _common import (
    MODEL_DIR,
    WORK,
    cap_onnx_ir_version,
    fetch_base_weights,
    hp,
    run_converter,
    stage_manifest_and_images,
    write_metadata,
)

# Seg_Size -> (rfdetr class name, native square resolution, decoder queries
# (num_queries == num_select, the device's top_k), input block = patch_size *
# num_windows). Pinned to detection_training.RFDETR_SEG_* by
# test_train_rfdetr_seg_static.py and test_rfdetr_seg_vocabulary_parity.py.
# All six sizes ship (owner decision D4); the published weights resolve for
# every one (spike S7).
SEG_SIZE_CLASSES = {
    "nano": ("RFDETRSegNano", 312, 100, 12),
    "small": ("RFDETRSegSmall", 384, 100, 24),
    "medium": ("RFDETRSegMedium", 432, 200, 24),
    "large": ("RFDETRSegLarge", 504, 200, 24),
    "xlarge": ("RFDETRSegXLarge", 624, 300, 24),
    "2xlarge": ("RFDETRSeg2XLarge", 768, 300, 24),
}
DEFAULT_SIZE = "small"

# Seg resolution bounds (Requirement 1.2 / 4.4): a multiple of the selected
# size's block within these; rfdetr's own ValueError at construction is the
# backstop inside det.build_model.
RESOLUTION_MIN = 240
RESOLUTION_MAX = 1152

# The mask head's grid is RESOLUTION // 4 (rfdetr mask_downsample_ratio).
MASK_DOWNSAMPLE = 4

# The fine-tunable checkpoint this entry point leaves in its artifact (same
# member as the detection trainer).
CHECKPOINT_MEMBER = det.CHECKPOINT_MEMBER

# Device manifest defaults. STAGE_TYPE is the default Output_Mode's stage
# type: hints only, the Portal record's output_mode decides the packaged one.
STAGE_TYPE = "rf_detr_instance_segmentation"
OUTPUT_MODE = "instance"
DEVICE_SCORE_THRESHOLD = 0.5
DEVICE_MASK_THRESHOLD = 0.5

# rfdetr's ONNX export names (export/main.py): "input"; dets, labels, masks.
ONNX_MASKS_NAME = "masks"

# Metric keys of the single `TEST METRICS: {...}` log line: the detection four
# (boxes) plus the two mask keys detection_training.RFDETR_SEG_METRIC_DEFINITIONS
# lifts ("test_map50": is not a substring of "test_mask_map50": ).
METRIC_KEYS = det.METRIC_KEYS + ("test_mask_map50", "test_mask_map50_95")
# rfdetr .evaluate() key -> our key (the split prefix is added per call). The
# only mask keys 1.10.1 returns are segm_mAP_50 / segm_mAP_50_95
# (coco_eval.py:128; confirmed live by spike S3).
_SEG_EVAL_KEY_MAP = {
    **det._EVAL_KEY_MAP,
    "segm_mAP_50": "test_mask_map50",
    "segm_mAP_50_95": "test_mask_map50_95",
}

# Instance_Manifest vocabulary (mirrors dda_manifest.py / the converter's
# detect_instance_attribute without importing either).
INSTANCE_ATTRIBUTE = "instance-segmentation"
INSTANCE_METADATA_TYPE = "groundtruth/instance-segmentation"
INSTANCE_MANIFEST_FATAL = (
    "FATAL: manifest line 1 carries no instance-segmentation attribute "
    "(bbox-only or anomaly-mask-ref manifests cannot train RF-DETR-Seg)")


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

def native_resolution(size: str) -> int:
    """The published Seg checkpoint's square resolution for a Seg_Size."""
    return SEG_SIZE_CLASSES[size][1]


def validate_seg_resolution(size: str, resolution) -> int:
    """Return `resolution` as an int, or exit FATAL when it is not a positive
    multiple of the size's input block (12 for nano, 24 otherwise) within
    [240, 1152] (Requirement 4.4; pure, checked before anything is downloaded).
    The detection rule of 32 is NOT applied."""
    if size not in SEG_SIZE_CLASSES:
        sys.exit(f"FATAL: RFDETR_SEG_SIZE must be one of {', '.join(SEG_SIZE_CLASSES)}; "
                 f"got {size!r}")
    block = SEG_SIZE_CLASSES[size][3]
    fatal = (f"FATAL: RESOLUTION must be a multiple of {block} between "
             f"{RESOLUTION_MIN} and {RESOLUTION_MAX} for RF-DETR-Seg {size}")
    try:
        r = int(resolution)
    except (TypeError, ValueError):
        sys.exit(f"{fatal} (got {resolution!r})")
    if r <= 0 or r % block or r < RESOLUTION_MIN or r > RESOLUTION_MAX:
        sys.exit(f"{fatal} (got {r})")
    return r


def read_config() -> SimpleNamespace:
    """Read the portal's hyperparameters from the environment: the detection
    set plus RFDETR_SEG_SIZE (default small). RESOLUTION defaults to the
    size's native resolution and is validated here, before any S3 download.
    No threshold is read: thresholds only shape the device manifest."""
    size = (hp("RFDETR_SEG_SIZE", DEFAULT_SIZE) or DEFAULT_SIZE).strip().lower()
    if size not in SEG_SIZE_CLASSES:
        sys.exit(f"FATAL: RFDETR_SEG_SIZE must be one of {', '.join(SEG_SIZE_CLASSES)}; "
                 f"got {size!r}")
    model_class, native, top_k, block = SEG_SIZE_CLASSES[size]
    resolution = hp("RESOLUTION")
    if resolution is None or str(resolution).strip() == "":
        resolution = native
    return SimpleNamespace(
        manifest_s3=hp("MANIFEST_S3"),
        images_s3=hp("IMAGES_S3"),
        size=size,
        model_class=model_class,
        native=native,
        top_k=top_k,
        block=block,
        resolution=validate_seg_resolution(size, resolution),
        epochs=hp("EPOCHS", "100", int),
        batch=hp("BATCH", "4", int),
        grad_accum=hp("GRAD_ACCUM", "4", int),
        lr=hp("LR", "1e-4", float),
        patience=hp("PATIENCE", "10", int),
        opset=hp("ONNX_OPSET", "17", int),
        base_weights_s3=hp("BASE_WEIGHTS_S3"),
        base_weights_member=hp("BASE_WEIGHTS_MEMBER"),
    )


# ---------------------------------------------------------------------------
# Dataset
# ---------------------------------------------------------------------------

def first_line_is_instance_manifest(line: str) -> bool:
    """True when a manifest line carries the instance-segmentation attribute
    (the DDA literal, or a Ground Truth job-named attribute whose
    `<name>-metadata.type` is groundtruth/instance-segmentation) -- the same
    rule the bundled converter's detect_instance_attribute applies (pure).
    A bbox-only line, an anomaly-mask-ref line or unparsable text is False."""
    try:
        entry = json.loads(line)
    except (TypeError, ValueError):
        return False
    if not isinstance(entry, dict):
        return False
    if INSTANCE_ATTRIBUTE in entry:
        return True
    for key, value in entry.items():
        if not key.endswith("-metadata") or not isinstance(value, dict):
            continue
        if str(value.get("type", "")).lower() == INSTANCE_METADATA_TYPE \
                and key[:-len("-metadata")] in entry:
            return True
    return False


def read_first_line(manifest: Path) -> str:
    """The first non-blank line of the manifest; FATAL when there is none."""
    with open(manifest, encoding="utf-8") as fh:
        for raw in fh:
            if raw.strip():
                return raw
    sys.exit(f"FATAL: manifest {manifest} is empty")


def stage_dataset(cfg) -> Tuple[Path, List[str]]:
    """Download manifest + images, require an instance manifest, build the
    RF-DETR COCO (RLE) layout, check splits (Requirement 4.2).

    det.stage_dataset is not reused: it has no hook for the first-line check,
    which must run before the converter so a bbox-only manifest fails with a
    precise message instead of the converter's generic one."""
    manifest, images = stage_manifest_and_images(cfg.manifest_s3, cfg.images_s3, WORK)
    if not first_line_is_instance_manifest(read_first_line(manifest)):
        sys.exit(INSTANCE_MANIFEST_FATAL)
    dataset = run_converter(manifest, images, WORK / "dataset", "coco",
                            coco_layout="rfdetr")
    det.assert_splits(dataset, ("train", "valid"))
    if not det._annotations_path(dataset, "test").is_file():
        print("WARN: no test split; TEST METRICS will come from the valid split",
              flush=True)
    class_names = det.read_class_names(dataset)
    if not class_names:
        sys.exit("FATAL: train split has no categories")
    print(f"dataset: {dataset}\nclasses ({len(class_names)}): {class_names}", flush=True)
    return dataset, class_names


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def seg_metrics_from_eval(result: Dict[str, Any], split: str = "test") -> Dict[str, float]:
    """Map rfdetr's .evaluate() dict ({"<split>/mAP_50": ..., "<split>/segm_mAP_50":
    ...}) onto the six TEST METRICS keys (pure; Requirement 4.7). A missing or
    non-numeric key becomes NaN -- the MetricDefinitions regex does not match
    NaN, so the metric is simply absent in SageMaker instead of a fake 0.0 --
    and one WARN line per such key makes the absence visible in the job log."""
    out: Dict[str, float] = {}
    for src, dst in _SEG_EVAL_KEY_MAP.items():
        value = result.get(f"{split}/{src}")
        if value is None:
            value = result.get(src)
        if value is None:
            print(f"WARN: evaluate() returned no {split}/{src}", flush=True)
            out[dst] = float("nan")
            continue
        try:
            out[dst] = float(value.item() if hasattr(value, "item") else value)
        except (TypeError, ValueError):
            print(f"WARN: evaluate() returned a non-numeric {split}/{src}: {value!r}",
                  flush=True)
            out[dst] = float("nan")
    return {k: out[k] for k in METRIC_KEYS}


def read_test_metrics(model, dataset_dir: Path, cfg) -> Dict[str, float]:
    """Evaluate the reloaded best checkpoint on the converter's leakage-safe
    test split and print the `TEST METRICS: {json}` line with the six keys.
    Falls back to the valid split (with a log line) when there is no test
    split; never fatal -- a missing metric must not lose the model."""
    metrics: Dict[str, float] = {}
    split = "test" if det._annotations_path(dataset_dir, "test").is_file() else "val"
    if split != "test":
        print("WARN: no test split; evaluating the valid split instead", flush=True)
    try:
        result = model.evaluate(
            split=split,
            dataset_dir=str(dataset_dir),
            batch_size=cfg.batch,
            output_dir=str(WORK / "eval"),
            tensorboard=False,
        )
        raw = {k: (v.item() if hasattr(v, "item") else v) for k, v in dict(result).items()}
        print(f"rfdetr evaluate({split}): {json.dumps(raw, default=str)}", flush=True)
        metrics = seg_metrics_from_eval(dict(result), split)
        print("TEST METRICS: " + json.dumps(metrics), flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"WARN: {split}-split evaluation failed: {e}", flush=True)
    return metrics


# ---------------------------------------------------------------------------
# ONNX: static output dims, contract verification
# ---------------------------------------------------------------------------

def apply_output_dims(graph_outputs, observed: Dict[str, Sequence[int]]) -> List[str]:
    """Write the observed run-time shapes into the graph outputs' declared
    dims (pure over the protobuf objects; the onnx-free half of
    stamp_static_output_dims). A symbolic or unset dim takes the observed
    value (`dim_param` cleared, `dim_value` set); a static dim must already
    equal it. Returns the names whose declaration changed. ValueError for an
    output without an observed shape, a rank mismatch or a static dim that
    contradicts the run."""
    changed: List[str] = []
    for out in graph_outputs:
        shape = observed.get(out.name)
        if shape is None:
            raise ValueError(f"no observed shape for ONNX output {out.name!r} "
                             f"(observed: {sorted(observed)})")
        dims = out.type.tensor_type.shape.dim
        if len(dims) != len(shape):
            raise ValueError(f"ONNX output {out.name!r} declares rank {len(dims)} but the "
                             f"runtime produced rank {len(shape)} {list(shape)}")
        touched = False
        for d, v in zip(dims, shape):
            v = int(v)
            if d.HasField("dim_value"):
                if d.dim_value != v:
                    raise ValueError(f"ONNX output {out.name!r} declares a static dim "
                                     f"{d.dim_value} where the runtime produced {v} "
                                     f"(declared {[x.dim_value if x.HasField('dim_value') else x.dim_param for x in dims]}, "
                                     f"observed {list(shape)})")
                continue
            d.ClearField("dim_param")
            d.dim_value = v
            touched = True
        if touched:
            changed.append(out.name)
    return changed


_NP_DTYPES = {
    "tensor(float)": "float32",
    "tensor(float16)": "float16",
    "tensor(double)": "float64",
    "tensor(int64)": "int64",
    "tensor(int32)": "int32",
    "tensor(uint8)": "uint8",
}


def stamp_static_output_dims(path: Path) -> Dict[str, List[int]]:
    """Make the exported graph's output dims static in place (spike S2 /
    mismatch M-1): the raw rfdetr 1.10.1 export declares `masks` with four
    symbolic dims although every runtime produces [1, Q, R/4, R/4], and
    onnxruntime 1.19.2 (the trainer's pin behind read_onnx_io) reports the
    symbolic names, so verify_seg_outputs -- and every protobuf reader in the
    Portal -- would reject a real export. Runs the graph once on zeros in
    this environment's onnxruntime, writes the observed shapes into
    graph.output, re-validates with onnx.checker and saves. IR version and
    outputs are unchanged (verified live in onnxruntime 1.30.0, 1.19.2 and
    1.16.3). Returns {output name: observed shape}."""
    import numpy as np
    import onnx
    import onnxruntime as ort

    sess = ort.InferenceSession(str(path), providers=["CPUExecutionProvider"])
    feeds = {}
    for i in sess.get_inputs():
        dtype = _NP_DTYPES.get(str(i.type), "float32")
        shape = [int(d) if isinstance(d, int) and d > 0 else 1 for d in i.shape]
        feeds[i.name] = np.zeros(shape, dtype=dtype)
    outputs = sess.run(None, feeds)
    observed = {o.name: [int(d) for d in arr.shape]
                for o, arr in zip(sess.get_outputs(), outputs)}
    model = onnx.load(str(path))
    changed = apply_output_dims(model.graph.output, observed)
    if changed:
        onnx.checker.check_model(model)
        onnx.save(model, str(path))
        print(f"stamped static output dims on {changed}: {observed}", flush=True)
    else:
        print(f"ONNX output dims already static: {observed}", flush=True)
    return observed


def verify_seg_outputs(outputs: Sequence[Any], num_classes: int, resolution: int,
                       ) -> Tuple[int, List[int], List[int], List[int], List[Optional[str]]]:
    """Check the exported graph is the three-tensor RF-DETR-Seg contract
    (pure; Requirement 4.5; sibling of det.verify_two_outputs).

    `outputs` is a list of shapes, or of (name, shape) pairs as read_onnx_io
    returns. Exactly three static-int outputs with batch 1 sharing Q: one
    rank-3 with last dim 4 (boxes, cxcywh), one rank-3 with last dim EXACTLY
    num_classes + 1 (logits; rfdetr allocates a trailing background slot --
    a num_classes-slot graph is an un-widened head and fails with the
    verify_two_outputs message), one rank-4 [1, Q, R/4, R/4] (mask logits).
    Boxes vs logits are told apart by shape; when num_classes + 1 == 4 the
    names dets/labels settle it and a nameless graph is FATAL. Returns
    (Q, boxes_shape, logits_shape, masks_shape, names_in_order) -- the names
    in graph order, which the metadata records as onnx_output_names so the
    device can resolve the same ambiguity. Raises ValueError with a FATAL
    message quoting every output otherwise."""
    parsed = [det._as_shape(o) for o in outputs]
    quoted = ", ".join(f"{n or '?'}={s}" for n, s in parsed)
    grid = int(resolution) // MASK_DOWNSAMPLE
    num_classes = int(num_classes)
    expected_logits = num_classes + 1
    expected = (f"[1, Q, 4] boxes + [1, Q, {expected_logits}] logits "
                f"(num_classes={num_classes} + 1 background slot) + [1, Q, {grid}, {grid}] masks")
    if len(parsed) != 3:
        raise ValueError(f"FATAL: expected exactly 3 ONNX outputs ({expected}), "
                         f"got {len(parsed)}: [{quoted}]")
    for _n, s in parsed:
        if len(s) not in (3, 4) or not all(det._is_int(d) for d in s):
            raise ValueError("FATAL: ONNX outputs must be static rank-3 (boxes, logits) and "
                             f"rank-4 (masks) tensors, got [{quoted}]")
        if s[0] != 1:
            raise ValueError(f"FATAL: ONNX outputs must have batch 1, got [{quoted}]")
    rank3 = [(n, s) for n, s in parsed if len(s) == 3]
    rank4 = [(n, s) for n, s in parsed if len(s) == 4]
    if len(rank3) != 2 or len(rank4) != 1:
        raise ValueError(f"FATAL: ONNX outputs [{quoted}] do not match {expected}: "
                         f"expected two rank-3 tensors and one rank-4 tensor")
    if len({s[1] for _n, s in parsed}) != 1:
        raise ValueError(f"FATAL: ONNX outputs disagree on Q (num_queries): [{quoted}]")
    masks_name, masks = rank4[0]
    if masks[2] != grid or masks[3] != grid:
        raise ValueError(f"FATAL: ONNX mask output {masks_name or '?'}={masks} is not "
                         f"[1, Q, {grid}, {grid}] (RESOLUTION {resolution} // {MASK_DOWNSAMPLE}); "
                         f"got [{quoted}]")

    names = [n for n, _s in rank3]
    shapes = [s for _n, s in rank3]
    boxes = logits = None
    if det.ONNX_BOXES_NAME in names and det.ONNX_LOGITS_NAME in names:
        boxes = shapes[names.index(det.ONNX_BOXES_NAME)]
        logits = shapes[names.index(det.ONNX_LOGITS_NAME)]
    elif det.ONNX_BOXES_NAME in names:
        boxes = shapes[names.index(det.ONNX_BOXES_NAME)]
        logits = shapes[1 - names.index(det.ONNX_BOXES_NAME)]
    elif det.ONNX_LOGITS_NAME in names:
        logits = shapes[names.index(det.ONNX_LOGITS_NAME)]
        boxes = shapes[1 - names.index(det.ONNX_LOGITS_NAME)]
    else:
        first, second = shapes
        if first[2] == 4 and second[2] != 4:
            boxes, logits = first, second
        elif second[2] == 4 and first[2] != 4:
            boxes, logits = second, first
        elif first[2] == 4 and second[2] == 4:
            raise ValueError(f"FATAL: ONNX outputs [{quoted}] are ambiguous (two "
                             f"[1, {first[1]}, 4] tensors, no dets/labels names)")
    if boxes is None or logits is None or boxes[2] != 4:
        raise ValueError(f"FATAL: ONNX output shapes [{quoted}] do not match {expected}")
    if logits[2] == num_classes:
        raise ValueError(
            f"FATAL: ONNX logits output {logits} has only num_classes={num_classes} slots; "
            f"expected [1, {logits[1]}, {expected_logits}] (num_classes + 1 background slot). "
            f"The detection head was not widened to the manifest's classes -- got "
            f"[{quoted}], expected [1, {logits[1]}, 4] + [1, {logits[1]}, {expected_logits}]")
    if logits[2] != expected_logits:
        raise ValueError(f"FATAL: ONNX output shapes [{quoted}] do not match {expected}")
    names_in_order = [n for n, _s in parsed]
    return (int(boxes[1]), [int(d) for d in boxes], [int(d) for d in logits],
            [int(d) for d in masks], names_in_order)


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------

def build_seg_metadata(cfg, class_names: List[str], metrics: Dict[str, float],
                       input_shape: List[int], shapes: Sequence[Sequence[int]],
                       names: Sequence[Optional[str]], top_k: int,
                       base: Optional[Path] = None) -> Dict[str, Any]:
    """training_metadata.json for an RF-DETR-Seg artifact (pure; Requirement
    4.6; design "Data contracts", training_metadata.json (Seg)).

    The detection keys of det.build_metadata with detection_arch
    rf_detr_seg / model_type instance_segmentation, rfdetr_seg_size, the
    three-entry onnx_output_shapes [boxes, logits, masks], onnx_output_names
    (graph order), mask_downsample_ratio and the hints the packager turns
    into the device manifest (default Output_Mode instance; the Portal
    record's output_mode decides the packaged one).
    """
    boxes_shape, logits_shape, masks_shape = shapes
    num_classes = len(class_names)
    meta: Dict[str, Any] = {
        "detection_arch": "rf_detr_seg",
        "model_type": "instance_segmentation",
        "rfdetr_seg_size": cfg.size,
        "model_class": cfg.model_class,
        "resolution": cfg.resolution,
        "num_classes": num_classes,
        "class_names": list(class_names),
        "epochs": cfg.epochs,
        "batch": cfg.batch,
        "grad_accum": cfg.grad_accum,
        "lr": cfg.lr,
        "patience": cfg.patience,
        "opset": cfg.opset,
        "onnx_opset": cfg.opset,
        "onnx_input_shape": list(input_shape),
        # [boxes, logits, masks]; logits is [1, Q, C + 1] -- the trailing slot
        # is rfdetr's never-positive background slot, class i == class_names[i].
        # verify_seg_outputs has already enforced C + 1 and the R/4 grid.
        "onnx_output_shapes": [list(boxes_shape), list(logits_shape), list(masks_shape)],
        "onnx_output_names": list(names),
        "logits_slots": int(logits_shape[2]),
        "background_slot": int(logits_shape[2]) - 1,
        "mask_downsample_ratio": MASK_DOWNSAMPLE,
        "top_k": int(top_k),
        # The published size class, or the S3 URI the run was fine-tuned from
        # when BASE_WEIGHTS_S3 was set (base_weights_member then names the
        # checkpoint inside that artifact).
        "base_weights": cfg.base_weights_s3 if base is not None else cfg.model_class,
        "metrics": dict(metrics),
        "manifest_s3": cfg.manifest_s3,
        "images_s3": cfg.images_s3,
        # The device MUST square-resize (no letterbox) and ImageNet-normalise
        # to match how this was trained; top_k set-selection, no NMS; masks
        # thresholded at mask_threshold on the sigmoid.
        "device_manifest_hints": {
            "layout": "rf_detr",
            "stage_type": STAGE_TYPE,
            "output_mode": OUTPUT_MODE,
            "network_input": cfg.resolution,
            "preserve_aspect": False,
            "normalize": True,
            "score_threshold": DEVICE_SCORE_THRESHOLD,
            "mask_threshold": DEVICE_MASK_THRESHOLD,
            "top_k": int(top_k),
        },
    }
    if base is not None:
        meta["base_weights_member"] = cfg.base_weights_member or CHECKPOINT_MEMBER
    return meta


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    cfg = read_config()
    if not cfg.manifest_s3:
        sys.exit("FATAL: MANIFEST_S3 is required")
    print(f"MANIFEST_S3={cfg.manifest_s3}\nIMAGES_S3={cfg.images_s3 or '(from source-ref)'}\n"
          f"RFDETR_SEG_SIZE={cfg.size} ({cfg.model_class}) RESOLUTION={cfg.resolution} "
          f"EPOCHS={cfg.epochs} BATCH={cfg.batch} GRAD_ACCUM={cfg.grad_accum} "
          f"LR={cfg.lr} PATIENCE={cfg.patience} ONNX_OPSET={cfg.opset}", flush=True)
    if cfg.base_weights_s3:
        print(f"BASE_WEIGHTS_S3={cfg.base_weights_s3} "
              f"BASE_WEIGHTS_MEMBER={cfg.base_weights_member or CHECKPOINT_MEMBER}",
              flush=True)

    dataset, class_names = stage_dataset(cfg)
    # None unless BASE_WEIGHTS_S3 is set -> published Seg COCO weights.
    base = fetch_base_weights(WORK / "base_weights", cfg.base_weights_s3,
                              cfg.base_weights_member, default_member=CHECKPOINT_MEMBER)
    if base is not None:
        det.log_checkpoint_classes(base, class_names)

    # The Seg class from SEG_SIZE_CLASSES through det's helpers: the
    # num_classes pin on the base-weights path, det.train_kwargs unchanged
    # (no seg loss knobs: TrainConfig is extra="forbid").
    Model = det.model_class(cfg)
    model = det.build_model(Model, cfg, base, num_classes=len(class_names))
    best = det.train_model(model, cfg, dataset, class_names)

    # Everything shipped comes from the best checkpoint, reloaded from disk:
    # metrics, ONNX and the checkpoint copy then describe the same weights.
    best_model = det.reload_best(Model, best, cfg)
    metrics = read_test_metrics(best_model, dataset, cfg)

    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    produced = det.export_onnx(best_model, cfg, WORK / "export")
    dst = MODEL_DIR / "model.onnx"
    shutil.copy2(produced, dst)
    print(f"exported {produced} -> {dst} ({dst.stat().st_size} bytes)", flush=True)
    # The raw export declares the mask dims symbolically: make them static
    # before the IR cap and before anything reads the protobuf.
    try:
        stamp_static_output_dims(dst)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"FATAL: could not stamp static output dims on {dst}: {e}")
    # Keep the IR the edge runtimes load, whatever onnx the export used.
    cap_onnx_ir_version(dst)

    try:
        inputs, outputs = det.read_onnx_io(dst)
    except Exception as e:  # noqa: BLE001
        sys.exit(f"FATAL: could not introspect exported ONNX {dst}: {e}")
    print(f"ONNX inputs : {inputs}\nONNX outputs: {outputs}", flush=True)
    try:
        input_shape = det.verify_input(inputs, cfg.resolution)
        top_k, boxes_shape, logits_shape, masks_shape, names = verify_seg_outputs(
            outputs, len(class_names), cfg.resolution)
    except ValueError as e:
        sys.exit(str(e))
    print(f"ONNX contract OK: boxes {boxes_shape} logits {logits_shape} masks {masks_shape} "
          f"names {names} (Q={top_k}, num_classes={len(class_names)} + 1 background slot)",
          flush=True)
    if top_k != cfg.top_k:
        print(f"WARN: Q={top_k} differs from the expected {cfg.top_k} queries of "
              f"{cfg.model_class}; top_k in the metadata follows the graph", flush=True)

    meta = build_seg_metadata(cfg, class_names, metrics, input_shape,
                              [boxes_shape, logits_shape, masks_shape], names, top_k, base)
    write_metadata(MODEL_DIR, meta)

    # Keep the fine-tunable checkpoint too: it is what a later run starts from
    # (BASE_WEIGHTS_S3 + BASE_WEIGHTS_MEMBER) and what you re-export from if
    # the resolution or opset needs changing.
    shutil.copy2(best, MODEL_DIR / CHECKPOINT_MEMBER)
    print(f"kept {best} -> {MODEL_DIR / CHECKPOINT_MEMBER}", flush=True)
    print("DONE", flush=True)


if __name__ == "__main__":
    main()
